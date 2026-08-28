"""Memory write router for system nodes (dream + memory_extractor).

[AutoC 2026-08-28] Why: system.dream and system.memory_extractor organize/extract
memory entries for other nodes (ereuna_main, bootstrap.coder, etc.), but namespace
isolation causes their save_memory/delete_memory calls to write into their own
directories (data/memory/system.dream/, data/memory/system.memory_extractor/)
instead of the source node's directory.

How: register a before_tool_call hook that intercepts save_memory and delete_memory
when the calling node is a system node. Two routing modes:

1. Dream mode (_memory_route table): maps book name -> {ns, ws}. Dream's pipeline
   scans all node directories and builds this table at task creation time.

2. Extractor mode (_memory_route_ns): a single target namespace string. The
   memory_extract handler captures the source entry node_id when preparing the
   extraction and passes it through input_data.

Purpose: memory writes land in the correct node/workspace directory without
modifying save_memory or delete_memory tools themselves.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.hooks.types import Handler, HookContext, HookResult

log = logging.getLogger(__name__)

_SYSTEM_NODES = frozenset({"system.dream", "system.memory_extractor"})
_ROUTABLE_TOOLS = frozenset({"save_memory", "delete_memory"})


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

        # Only intercept save_memory / delete_memory
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

        # Mode 1: Dream route table (book -> {ns, ws})
        memory_route: dict[str, Any] = task_context.get("_memory_route") or {}
        if memory_route:
            book = str(tool_args.get("book") or "default").strip()
            target = memory_route.get(book)
            if target is None:
                # Book not in route table (new book created by dream).
                # Fall back to the first route entry's namespace.
                _first = next(iter(memory_route.values()), None)
                if _first:
                    target = _first
            if target:
                target_ns = str(target.get("ns") or "").strip()
                target_ws = str(target.get("ws") or "").strip()

        # Mode 2: Direct namespace from memory_extractor
        if not target_ns:
            _direct_ns = str(task_context.get("_memory_route_ns") or "").strip()
            if _direct_ns:
                target_ns = _direct_ns

        if not target_ns:
            return None

        # Rewrite ToolContext to redirect the write
        old_node_id = getattr(tool_ctx, "_node_id", "") or getattr(tool_ctx, "node_id", "")
        tool_ctx._node_id = target_ns  # type: ignore[attr-defined]
        tool_ctx.node_id = target_ns

        # Clear _node_extra memory_book so _effective_memory_ns falls back to _node_id
        if hasattr(tool_ctx, "_node_extra"):
            tool_ctx._node_extra = {}  # type: ignore[attr-defined]

        if target_ws:
            # Strip the @ prefix for workspace_name (save_memory adds it back)
            tool_ctx.workspace_name = target_ws.lstrip("@")

        log.info(
            "[dream_router] redirected %s book=%s from ns=%s to ns=%s ws=%s",
            tool_name,
            tool_args.get("book", "default"),
            old_node_id,
            target_ns,
            target_ws or "(node-level)",
        )
        return None  # Allow execution to proceed with patched context
