"""Dream memory router: redirect save_memory/delete_memory writes to source nodes.

[AutoC 2026-08-28] Why: the system.dream node organizes memory entries from all
nodes (ereuna_main, bootstrap.coder, etc.), but namespace isolation causes its
save_memory/delete_memory calls to write into data/memory/system.dream/ instead
of the original source directory.

How: register a before_tool_call hook that intercepts save_memory and delete_memory
when the calling node is system.dream. The hook reads the _memory_route table from
task_context (built by dream.py during pipeline setup) and rewrites ToolContext's
_node_id and workspace_name so save_memory's existing namespace logic writes to
the correct target. The route table maps book name -> {ns, ws}.

Purpose: dream results land in the correct node/workspace directory without
modifying save_memory or delete_memory tools themselves.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.hooks.types import Handler, HookContext, HookResult

log = logging.getLogger(__name__)

_DREAM_NODE_PREFIX = "system.dream"
_ROUTABLE_TOOLS = frozenset({"save_memory", "delete_memory"})


class DreamMemoryRouter(Handler):
    """Redirect dream node memory writes to the original source namespace."""

    name = "dream_memory_router"
    priority = 200  # Run before approval (100) so ctx is patched before execution

    async def handle(self, ctx: HookContext) -> HookResult | None:
        # Only intercept system.dream node calls
        node = ctx.node
        if node is None:
            return None
        node_id = str(getattr(node, "id", "") or "").strip()
        if not node_id.startswith(_DREAM_NODE_PREFIX):
            return None

        # Only intercept save_memory / delete_memory
        tool_call = ctx.tool_call
        if tool_call is None:
            return None
        tool_name = str(getattr(tool_call, "name", "") or "").strip()
        if tool_name not in _ROUTABLE_TOOLS:
            return None

        # Read route table from task_context
        rctx = ctx.rctx
        if rctx is None:
            return None
        task_context = getattr(rctx, "task_context", None) or {}
        memory_route: dict[str, Any] = task_context.get("_memory_route") or {}
        if not memory_route:
            return None

        # Get the tool_ctx reference from extra (added by ai_step.py)
        tool_ctx = ctx.extra.get("tool_ctx")
        tool_args = ctx.extra.get("tool_args") or {}
        if tool_ctx is None:
            return None

        # Resolve target from book name
        book = str(tool_args.get("book") or "default").strip()
        target = memory_route.get(book)
        if target is None:
            # Book not in route table (new book created by dream).
            # Fall back to the first route entry's namespace if available,
            # since dream is likely operating on the primary node's memory.
            _first = next(iter(memory_route.values()), None)
            if _first:
                target = _first
            else:
                return None

        target_ns = str(target.get("ns") or "").strip()
        target_ws = str(target.get("ws") or "").strip()

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
            tool_name, book, old_node_id, target_ns, target_ws,
        )
        return None  # Allow execution to proceed with patched context
