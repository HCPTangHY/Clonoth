from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .types import HookResult
from ..registry_core import DisposalLedger

logger = logging.getLogger(__name__)

# Why: hook points were string literals scattered across call sites, so neither
# plugin authors nor reviewers could enumerate them or learn their contracts.
# How: one table documents each point's trigger position, the data available in
# ctx.extra, and what a handler may do; register() warns on undeclared points.
# Purpose: make the hook point set auditable; adding a point means adding one
# entry here plus one fire/afire call at the trigger site.
HOOK_POINTS: dict[str, str] = {
    # ---- engine inference (async afire) ----
    "before_llm_call": (
        "每次 provider 调用前、L2 格式化与图片解析之后触发。"
        "extra: workspace_root。ctx.messages 是本次请求的 L2 产物（每次新建），"
        "handler 可原地改写或替换 ctx.messages，仅影响本次出站请求，"
        "不影响 ls.messages 与 JSONL 落库。返回 action 终止任务。"
        "典型用途：@文件引用展开、请求级脱敏。"
    ),
    "before_prompt_build": (
        "初始 messages 组装完成后、发送 LLM 前触发。"
        "extra: runtime_cfg/instruction_text/history/attachments/system_prompt。"
        "handler 可重建 ctx.messages；返回 action 非空则终止任务。"
        "注意：向 prompt 贡献内容应优先用 prompt_sections 声明型注册面。"
    ),
    "before_step": "每轮推理开始前触发。extra: step 序号等。返回 action 可终止。",
    "after_llm_call": "LLM 返回后触发。extra: response/usage。可读取用量做统计。",
    "before_tool_call": "工具执行前触发。extra: tool_name/arguments。返回 block 可拒绝执行。",
    "terminal_tool": (
        "终止型伪工具（finish/ask）实际执行前触发。extra: loop_state/terminal_call。"
        "handler 返回 intercepted=True 表示已写入拦截 tool_result，循环跳过本次执行"
        "但任务继续；返回 action 则直接终止任务（如 PREEMPTED）；返回 None 放行执行。"
    ),
    "execute_tool": (
        "真实工具执行策略点：结构化 start 事件发出后、实际执行前触发。"
        "extra: loop_state/tool_name/tool_args/tool_ctx/spec/call_index/t0。"
        "handler 可返回 execution（awaitable 或已完成值）：解析为工具结果，或 "
        "{'async_started': True, 'async_id', 'summary', 'raw_inline', 'handoff_message'} "
        "标记（循环写占位 entry、发 tool_call_end[async_started]、真实结果经 preempt 回传）。"
        "首个非空 execution 生效；是否允许执行由 before_tool_call 决定，此点不承担授权。"
    ),
    "after_tool_call": (
        "每个同步真实工具执行后、结构化格式化之后、事件与 entry 写回之前触发。"
        "extra: loop_state/tool_name/tool_args/tool_result/raw_inline/step_inline_state/"
        "call_index/tool_attachments。handler 可通过 channels 的 result_override 键"
        "（raw_inline/truncated/ref）改写模型可见的 inline 文本与截断元数据（spill 策略），"
        "或收集附件；返回 action 终止任务。"
    ),
    "before_response": "纯文本回复发出前触发。extra: text。可修改或拦截。",
    "on_task_end": "任务正常结束时触发。extra: result/status。用于收尾与统计。",
    "on_task_error": "任务异常时触发。extra: error/traceback。用于错误快照。",
    # ---- supervisor (sync fire) ----
    "on_inbound_message": "外部消息进入路由前触发。extra: message/conversation_key。可改写或阻断。",
    "on_schedule_tick": "定时任务触发时触发。extra: schedule_id/task。",
    "on_entry_task_complete": "入口任务完成时触发。用于结果回收与子任务编排。",
}


def _copy_plugin_meta(meta: dict) -> dict:
    """Copy plugin metadata without assuming every extra value is deep-copyable."""
    # Why: PLUGIN_META has simple documented fields, but plugins may publish extra
    # values. How: copy the dict and clone known mutable list fields. Purpose:
    # protect registry state without making unusual extra values break plugin loading.
    copied = dict(meta)
    for key in ("hooks", "hook_points"):
        if isinstance(copied.get(key), list):
            copied[key] = list(copied[key])
    return copied


@dataclass
class _RegisteredHook:
    """One normalized hook registration entry."""

    # Why: engine handlers and supervisor handlers now share one registry. How:
    # store the normalized callable, display name, and priority together. Purpose:
    # keep execution and idempotent replacement independent of handler shape.
    priority: int
    name: str
    callback: Any


class HookRegistry:
    """Unified registry for engine and supervisor hook handlers.

    Why: built-in hook handlers now live in one package and are discovered by
    metadata. How: accept either objects with handle(ctx) or plain callables, then
    expose both sync fire() and async afire(). Purpose: let engine and supervisor
    use the same registration and discovery path while keeping separate process-local
    registry instances.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[_RegisteredHook]] = {}
        # Why: external and built-in plugin files can declare metadata that should
        # be visible after startup. How: keep one normalized dict per plugin name.
        # Purpose: expose loaded plugin information without changing handler execution.
        self._loaded_plugins: list[dict] = []
        # Why: every registration must leave a matching undo operation so a plugin
        # can be unloaded without human recall of what it touched. How: disposers
        # are archived in the shared DisposalLedger (engine/registry_core.py);
        # while a loader is inside collecting(name), each register() records its
        # disposer under that plugin. Purpose: unloading a plugin is "call its
        # disposers in reverse order", not "remember what it changed".
        self._ledger = DisposalLedger()

    def collecting(self, plugin_name: str) -> Any:
        """Attribute register() disposers created inside the block to one plugin."""
        return self._ledger.collecting(plugin_name)

    def add_plugin_disposer(self, plugin_name: str, disposer: Callable[[], None]) -> None:
        """Archive an externally created disposer under a plugin name."""
        self._ledger.add(plugin_name, disposer)

    def unload_plugin(self, plugin_name: str) -> dict[str, Any]:
        """Undo every registration owned by one plugin, in reverse order.

        Why: plugin hot-unload must not rely on knowing what a plugin registered.
        How: pop the archived disposer list, call each entry last-in-first-out,
        and drop the plugin metadata record. Purpose: make unload a mechanical
        operation with per-disposer error isolation.
        """
        name = (plugin_name or "").strip()
        result = self._ledger.unload(name)
        removed_meta = False
        for index, plugin in enumerate(self._loaded_plugins):
            if plugin.get("name") == name:
                self._loaded_plugins.pop(index)
                removed_meta = True
                break
        result["removed_meta"] = removed_meta
        return result

    def register(self, hook_point: str, handler: Any, priority: int | None = None) -> Callable[[], None]:
        """Register one handler to a hook point and return its disposer.

        Why: engine handlers are objects with .handle(ctx), while supervisor
        handlers are bound methods. How: normalize both forms to a callable and
        derive name/priority from the handler or its bound instance. Purpose: make
        repeated auto-discovery idempotent across all built-in hook points.

        The returned disposer removes only this exact registration: it matches on
        both handler name and callback identity, so a later same-name replacement
        is not accidentally removed by an earlier disposer.
        """
        point = _normalize_hook_point(hook_point)
        if point not in HOOK_POINTS:
            # Why: undeclared points are usually typos or zombie extension spots.
            # How: warn but still register, so experimental points stay possible.
            # Purpose: surface drift early without breaking dynamic use cases.
            logger.warning("Hook point %r is not declared in HOOK_POINTS", point)
        callback = _handler_callback(handler)
        name = _handler_name(handler)
        resolved_priority = _handler_priority(handler, priority)
        handlers = self._hooks.setdefault(point, [])
        handlers[:] = [entry for entry in handlers if entry.name != name]
        entry = _RegisteredHook(priority=resolved_priority, name=name, callback=callback)
        handlers.append(entry)
        handlers.sort(key=lambda item: item.priority, reverse=True)

        def _dispose() -> None:
            current = self._hooks.get(point)
            if not current:
                return
            current[:] = [item for item in current if not (item.name == name and item.callback == callback)]
            if not current:
                self._hooks.pop(point, None)

        self._ledger.record(_dispose)
        return _dispose

    def unregister(self, hook_point: str, handler_name: str) -> bool:
        """Remove a handler by name and report whether anything changed."""
        point = _normalize_hook_point(hook_point)
        handlers = self._hooks.get(point)
        if not handlers:
            return False
        before = len(handlers)
        handlers[:] = [entry for entry in handlers if entry.name != handler_name]
        if not handlers:
            self._hooks.pop(point, None)
        return len(handlers) != before

    def register_plugin_meta(self, meta: dict) -> None:
        """Register or replace metadata for one loaded plugin."""
        # Why: startup can scan the same built-in or external plugin repeatedly.
        # How: replace a previous record with the same name instead of appending a
        # duplicate, while storing a copy of the caller's dict. Purpose: make
        # metadata registration idempotent like handler registration.
        stored = _copy_plugin_meta(meta)
        name = stored.get("name") or stored.get("handler_class") or "unknown"
        stored["name"] = name
        for index, plugin in enumerate(self._loaded_plugins):
            if plugin.get("name") == name:
                self._loaded_plugins[index] = stored
                return
        self._loaded_plugins.append(stored)

    def list_plugins(self) -> list[dict]:
        """Return metadata for plugins that loaded successfully."""
        # Why: callers should inspect plugin state without mutating registry internals.
        # How: return copied metadata dicts and copied hook lists. Purpose: keep the
        # registry state owned by HookRegistry.
        return [_copy_plugin_meta(plugin) for plugin in self._loaded_plugins]

    def fire(self, hook_point: str, ctx: Any) -> HookResult:
        """Synchronously run handlers for one hook point.

        Why: supervisor hook points run on synchronous routing and scheduler paths.
        How: call registered callbacks directly and apply the same stop rules as
        engine hooks. Purpose: replace the old supervisor-only registry without making
        supervisor code async.
        """
        modified = False
        channels: dict[str, Any] = {}
        _next_called = True  # 默认放行，兼容旧 handler
        for entry in list(self._hooks.get(_normalize_hook_point(hook_point), [])):
            if not _next_called:
                break
            _next_called = True  # 每次重置；只有 handler 显式设 ctx._next_skipped 才停
            try:
                result = entry.callback(ctx)
                if inspect.isawaitable(result):
                    _close_awaitable(result)
                    raise RuntimeError("async hook handler registered on sync fire(); use afire()")
                stop_result, modified = _process_hook_result(result, modified, channels)
                if stop_result is not None:
                    return stop_result
                # 瀑布语义：handler 可以通过 ctx.stop_chain() 终止后续 handler 执行
                if getattr(ctx, '_chain_stopped', False):
                    break
            except Exception as exc:
                logger.warning("Hook %s.%s failed: %s", hook_point, entry.name, exc)
        return HookResult(modified=modified, channels=channels)

    async def afire(self, hook_point: str, ctx: Any) -> HookResult:
        """Asynchronously run handlers for one hook point."""
        # Why: engine inference handlers can be async and often await runtime checks.
        # How: call each normalized callback and await awaitable results. Purpose:
        # keep engine control-flow semantics while sharing registration with sync hooks.
        modified = False
        channels: dict[str, Any] = {}
        for entry in list(self._hooks.get(_normalize_hook_point(hook_point), [])):
            try:
                result = entry.callback(ctx)
                if inspect.isawaitable(result):
                    result = await result
                stop_result, modified = _process_hook_result(result, modified, channels)
                if stop_result is not None:
                    return stop_result
                if getattr(ctx, '_chain_stopped', False):
                    break
            except Exception as exc:
                logger.warning("Hook %s.%s failed: %s", hook_point, entry.name, exc)
        return HookResult(modified=modified, channels=channels)

    def list_hooks(self) -> dict[str, list[str]]:
        """Return registered hook points and handler names."""
        return {
            hook_point: [entry.name for entry in handlers]
            for hook_point, handlers in self._hooks.items()
        }


def _normalize_hook_point(hook_point: Any) -> str:
    """Convert enum-like or string hook point values to the registry key."""
    # Why: older supervisor code used enum values while the unified registry uses
    # globally unique strings. How: read .value when present and stringify the result.
    # Purpose: keep the transition tolerant without retaining supervisor enums.
    value = getattr(hook_point, "value", hook_point)
    return str(value)


def _handler_callback(handler: Any) -> Any:
    """Return the callable used to execute one handler."""
    # Why: engine handlers are objects with handle(ctx), while supervisor handlers
    # are already bound methods. How: prefer handle when present, otherwise accept
    # the handler itself if callable. Purpose: normalize execution at registration.
    handle = getattr(handler, "handle", None)
    if callable(handle):
        return handle
    if callable(handler):
        return handler
    raise TypeError(f"Hook handler is not callable: {handler!r}")


def _handler_name(handler: Any) -> str:
    """Derive a stable display and idempotency name for a handler."""
    # Why: auto-discovery registers bound methods, whose method name would otherwise
    # hide the owning handler identity. How: prefer explicit name on the handler or
    # bound instance, then fall back to callable metadata. Purpose: repeated scans
    # replace the same handler instead of accumulating duplicates.
    owner = getattr(handler, "__self__", None)
    for source in (handler, owner):
        explicit = getattr(source, "name", None)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
    qualname = getattr(handler, "__qualname__", None)
    if isinstance(qualname, str) and qualname.strip():
        return qualname.strip()
    name = getattr(handler, "__name__", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return handler.__class__.__qualname__


def _handler_priority(handler: Any, priority: int | None) -> int:
    """Resolve handler priority from explicit argument or handler attributes."""
    # Why: existing external plugins rely on handler.priority when they omit a
    # register() priority argument. How: prefer the explicit argument, then the
    # handler or bound instance attribute, then default to 100. Purpose: preserve
    # old engine ordering while supporting supervisor-style callable registration.
    if priority is not None:
        return int(priority)
    owner = getattr(handler, "__self__", None)
    for source in (handler, owner):
        value = getattr(source, "priority", None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                break
    return 100


# [AutoC 2026-08-23] Channel merge semantics by name. Why: hook points compose
# channel values differently — execution is single-owner, result_override merges
# per key, intercepted ORs. How: one table consulted at aggregation time;
# unknown channels default to "first" so a new channel needs no framework change
# unless it composes. Purpose: adding a channel is a data entry, not a schema
# change on HookResult.
CHANNEL_SEMANTICS: dict[str, str] = {
    "execution": "first",
    "result_override": "merge_dict",
    "intercepted": "or",
}


def _process_hook_result(
    result: Any, modified: bool, channels: dict[str, Any],
) -> tuple[Any | None, bool]:
    """Apply shared hook-result chain rules."""
    # Why: sync and async fire must stop on the same result shapes. How: use duck
    # typing for HookResult-compatible objects and aggregate non-terminal mutation.
    # Purpose: support engine.builtin.result.HookResultLike without importing it here.
    if result is None:
        return None, modified
    result_modified = bool(getattr(result, "modified", False))
    modified = modified or result_modified
    for key, value in (getattr(result, "channels", None) or {}).items():
        semantic = CHANNEL_SEMANTICS.get(key, "first")
        if semantic == "merge_dict":
            if isinstance(value, dict) and value:
                existing = channels.get(key)
                merged = dict(existing) if isinstance(existing, dict) else {}
                merged.update(value)
                channels[key] = merged
        elif semantic == "or":
            channels[key] = bool(channels.get(key)) or bool(value)
        else:  # "first": single-owner, first non-None wins
            if value is not None and channels.get(key) is None:
                channels[key] = value
    should_stop = bool(getattr(result, "block", False) or getattr(result, "skip_step", False) or getattr(result, "action", None) is not None)
    if should_stop:
        if modified and not result_modified and hasattr(result, "modified"):
            result.modified = True
        # terminated chains still carry the merged channels so the fire site
        # sees the same aggregate shape as a completed chain
        if hasattr(result, "channels"):
            result.channels = dict(channels)
        return result, modified
    return None, modified


def _close_awaitable(value: Any) -> None:
    """Best-effort close for a coroutine accidentally returned in sync fire."""
    # Why: calling an async handler from sync fire creates an unawaited coroutine.
    # How: close coroutine-like values when possible before logging the misuse.
    # Purpose: avoid RuntimeWarning noise while making the incorrect call visible.
    close = getattr(value, "close", None)
    if callable(close):
        close()
