"""Meta-only built-in plugin: declares the system.turn_summarizer node.

[AutoC 2026-08-30] Why: engine/system_nodes/ was a compromise location; the
turn summarizer node is dispatched directly by supervisor/task_router, so it
has no hook-handler owner plugin. How: a declaration-only plugin — the
built-in loader accepts PLUGIN_META without handler_class when it carries
declarative surfaces (nodes), registering them inside collecting() so unload
reverts through the shared ledger. Purpose: every kernel node ships from a
plugin and can be overridden/removed like any other capability.
"""

from __future__ import annotations

PLUGIN_META = {
    "name": "turn_summary",
    "description": "轮摘要节点声明（system.turn_summarizer），由 supervisor 任务路由在 task 完成后触发。",
    "nodes": [{"file": "nodes/system.turn_summarizer.yaml"}],
}
