"""Memory write router for system nodes (dream + memory_extractor).

[AutoC 2026-08-28] Why: system.dream and system.memory_extractor organize/extract
memory entries for other nodes, but namespace isolation causes their
manage_memory calls to write into their own directories
(data/memory/system.dream/, data/memory/system.memory_extractor/) instead of the
source node's directory.

How: register a before_tool_call hook that intercepts manage_memory when the
calling node is a system node. Two routing modes:

1. Dream mode (_memory_route table): maps book name (and exact "book/id" for
   updates/deletes) -> {ns, ws}. Dream's pipeline scans all node directories and
   builds this table at task creation time.

2. Extractor mode (_memory_route_ns): a single target namespace string. The
   memory_extract handler captures the source entry node_id when preparing the
   extraction and passes it through input_data.

Purpose: memory writes land in the correct node/workspace directory without
modifying the memory implementation itself. The real caller node id is preserved
on the context so policy approval and audit see system.dream /
system.memory_extractor rather than the redirected namespace.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.hooks.types import Handler, HookContext, HookResult

log = logging.getLogger(__name__)

_SYSTEM_NODES = frozenset({"system.dream", "system.memory_extractor"})
# Only the unified entry point is routed; retired CRUD names stay unregistered.
_ROUTABLE_TOOLS = frozenset({"manage_memory"})


def _resolve_memory_action(tool_args: dict[str, Any]) -> str:
    """Return the action manage_memory will actually run for these arguments.

    Mirrors the tool contract: only an explicit ``action`` selects a mutation,
    anything else is a read, so routing never guesses a write.
    """
    act = str(tool_args.get("action") or "").strip().lower()
    return act or "list"


class DreamMemoryRouter(Handler):
    """Redirect system node memory writes to the original source namespace."""

    name = "dream_memory_router"
    priority = 200  # Run before approval (100) so ctx is patched before execution

    async def handle(self, ctx: HookContext) -> HookResult | None:
        # Only intercept system node calls
        node = ctx.node
        if node is None:
            return None
        node_id = str(getattr(node, "id", "") or "").strip()
        if node_id not in _SYSTEM_NODES:
            return None

        # Only intercept manage_memory
        tool_call = ctx.tool_call
        if tool_call is None:
            return None
        tool_name = str(getattr(tool_call, "name", "") or "").strip()
        if tool_name not in _ROUTABLE_TOOLS:
            return None

        # Get the tool_ctx reference from extra (added by ai_step.py)
        tool_ctx = ctx.extra.get("tool_ctx")
        tool_args = ctx.extra.get("tool_args") or {}
        if tool_ctx is None:
            return None

        rctx = ctx.rctx
        if rctx is None:
            return None
        task_context = getattr(rctx, "task_context", None) or {}

        # --- Route resolution ---
        target_ns = ""
        target_ws = ""

        eff_action = _resolve_memory_action(tool_args)

        # Mode 1: Dream route table ('book/id' for exact entries, 'book' fallback)
        memory_route: dict[str, Any] = task_context.get("_memory_route") or {}
        if memory_route:
            book = str(tool_args.get("book") or "default").strip()
            mid = str(tool_args.get("id") or "").strip()
            target = None
            if mid and f"{book}/{mid}" in memory_route and memory_route[f"{book}/{mid}"] is None:
                return HookResult(block=True, reason="Memory identity occurs in multiple scopes",
                                  error_message="Ambiguous memory book/id; no scope was selected.")
            if mid:
                target = memory_route.get(f"{book}/{mid}")
            exact_target = target is not None
            if target is None:
                target = memory_route.get(book)
            if target is None:
                # Book not in route table (new book created by dream).
                # Fall back to a node-level entry, then to any entry.
                _first = next(
                    (
                        v
                        for v in memory_route.values()
                        if isinstance(v, dict) and not v.get("ws")
                    ),
                    None,
                ) or next(iter(memory_route.values()), None)
                if _first:
                    target = _first
            if target:
                target_ns = str(target.get("ns") or "").strip()
                # Updates of an identified entry keep its original scope. Only
                # a new entry using the book fallback follows the node-level policy.
                if node_id == "system.dream" and eff_action == "upsert" and not exact_target:
                    target_ws = ""
                else:
                    target_ws = str(target.get("ws") or "").strip()

        # Mode 2: Direct namespace from memory_extractor
        if not target_ns:
            _direct_ns = str(task_context.get("_memory_route_ns") or "").strip()
            if _direct_ns:
                target_ns = _direct_ns
                # [fix 2026-09-04] Dream preprocessing also carries the session's
                # workspace name; scope the write to the @ws directory when set.
                _direct_ws = str(task_context.get("_memory_route_ws") or "").strip()
                if _direct_ws:
                    target_ws = _direct_ws

        if not target_ns:
            return None

        # Route storage independently of caller identity and active workspace.
        # Neither approval attribution nor later tools may inherit the target's
        # identity/workspace merely because this call accesses its memory.
        old_node_id = getattr(tool_ctx, "_node_id", "") or getattr(tool_ctx, "node_id", "")
        tool_ctx._caller_node_id = node_id
        tool_ctx._memory_target = (target_ns, target_ws.lstrip("@"))
        extra = getattr(tool_ctx, "_node_extra", None)
        if isinstance(extra, dict):
            tool_ctx._node_extra = {key: value for key, value in extra.items() if key != "memory_book"}

        log.info(
            "[dream_router] redirected %s action=%s book=%s from ns=%s to ns=%s ws=%s",
            tool_name,
            eff_action,
            tool_args.get("book", "default"),
            old_node_id,
            target_ns,
            target_ws or "(node-level)",
        )
        return None  # Allow execution to proceed with patched context
