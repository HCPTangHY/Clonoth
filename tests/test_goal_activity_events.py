"""Execution identity must accompany activity events, not only final text."""
import asyncio
from types import SimpleNamespace
from pathlib import Path

from engine.context import RunContext


def test_tool_events_carry_claim_identity():
    posted = []

    class HTTP:
        async def post(self, url, **kwargs):
            posted.append(kwargs['json'])
            return SimpleNamespace(raise_for_status=lambda: None)

    async def run():
        ctx = RunContext(workspace_root=Path('.'), worker_id='worker', http=HTTP(), llm_http=HTTP(),
                         supervisor_url='http://test', session_id='session', task_id='task', execution_id='claim')
        for kind in ['tool_call', 'tool_result', 'thinking', 'assistant_text']:
            await ctx.emit_event(kind, {})
        assert all(row['payload']['execution_id'] == 'claim' for row in posted)
        assert all(row['payload']['task_id'] == 'task' for row in posted)
    asyncio.run(run())
