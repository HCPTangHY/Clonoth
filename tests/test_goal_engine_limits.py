"""Actual runner restrictions and real tool dispatch authorization for review tasks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from engine import runner
from engine.inference import ai_step
from engine.inference.pseudo_tools import allowed_pseudo_names, build_node_tool_specs
from engine.node import Node, ToolAccess
from engine.protocol import TaskAction
from providers.base import ProviderResponse
from engine.hooks import HookResult
from test_dynamic_history_foundation import run_task
from test_engine_execution_migration import FakeRegistry, make_ls, no_hook, response, spec, tool_call


def test_runner_strips_system_task_capabilities(tmp_path, monkeypatch):
    cap = run_task(tmp_path, monkeypatch, input_data={
        'instruction': 'review', '_system_task': True,
        '_execution_options': {'model': 'review-model', 'max_steps': 1}})
    node, ctx = cap['node'], cap['rctx']
    assert node.model == 'review-model'
    assert node.control_tools == 'none' and node.delegate_targets == []
    assert node.output == '' and node.output_mode == 'hybrid'
    assert node.tool_access.mode == node.skill_access.mode == node.memory_access.mode == 'none'
    assert ctx.is_system_task and ctx.task_context['execution_max_steps'] == 1


def test_hallucinated_review_tools_never_execute(tmp_path):
    async def run():
        registry = FakeRegistry({'execute_command': spec('execute_command')})
        node = Node(id='ide.goal_reviewer', type='ai', control_tools='none', tool_access=ToolAccess(mode='none'))
        real, tools = build_node_tool_specs(registry=registry, node=node)
        pseudo = allowed_pseudo_names(tools, node)
        assert not real and not tools and not pseudo
        ls = make_ls(tmp_path, registry=registry, node=node, allowed_real=real, allowed_pseudo=pseudo)
        calls = response(tool_call('c1', 'execute_command', {'command': 'false'}),
                         tool_call('c2', 'dispatch_to', {'target': 'main'}),
                         tool_call('c3', 'finish', {'text': 'fake'}))
        with patch.object(ai_step.hook_registry, 'afire', side_effect=no_hook), \
             patch.object(ai_step, '_handle_pseudo_tool', new=AsyncMock()) as handle:
            assert await ai_step._handle_tool_calls(ls, calls, 0) is None
        assert registry.started == []
        handle.assert_not_called()
    asyncio.run(run())


def test_system_review_enforces_one_inference_step(tmp_path):
    async def run():
        rctx = SimpleNamespace(workspace_root=tmp_path, task_context={'execution_max_steps': 1},
            is_system_task=True, session_id='review', completed_steps=0, response_info={})
        node = Node(id='ide.goal_reviewer', type='ai', control_tools='none')
        provider = SimpleNamespace(name='test', model='test')
        ls = SimpleNamespace(rctx=rctx, node=node, provider=provider, messages=[], openai_tools=[],
            use_stream=False, last_usage={}, last_prompt_tokens=0)
        response = ProviderResponse(ok=True, text='retry me')
        async def fire(point, context):
            if point == 'before_response':
                return HookResult(modified=True)
            if point == 'on_task_error':
                return HookResult(action=TaskAction(action='fail', node_id=node.id, error='limited'))
            return HookResult()
        with patch.object(ai_step, 'auto_discover_and_register'), \
             patch.object(ai_step, 'load_external_plugins'), \
             patch.object(ai_step, 'load_runtime_config', return_value={'engine': {'max_steps': 32}}), \
             patch.object(ai_step, 'assemble_initial_messages', return_value=([], False, [])), \
             patch.object(ai_step, 'build_node_tool_specs', return_value=(set(), [])), \
             patch.object(ai_step, 'allowed_pseudo_names', return_value=set()), \
             patch.object(ai_step, '_build_loop_state', return_value=ls), \
             patch.object(ai_step, '_call_llm_with_retry', new=AsyncMock(return_value=response)) as call, \
             patch.object(ai_step.hook_registry, 'afire', side_effect=fire):
            result = await ai_step.run_ai_node(rctx=rctx, provider=provider, registry=Mock(),
                node=node, instruction='review', history=[])
        assert result.action == 'fail'
        assert call.await_count == rctx.completed_steps == 1
    asyncio.run(run())
