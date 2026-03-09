"""验证 task 化后的 AI 节点执行逻辑。

用法：python scripts/verify_engine.py

不需要真实 LLM 或 Supervisor，全部用 mock。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

os.environ.setdefault("OPENAI_API_KEY", "mock-key")
os.environ.setdefault("OPENAI_MODEL", "mock-model")

from providers.base import ProviderResponse, ToolCall
from providers.openai import OpenAIProvider

from engine.graph import allowed_outcomes, load_workflow, next_node
from engine.node import load_node
from engine.prompt import assemble_prompt


class MockProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._orig_chat = OpenAIProvider.chat
        self._orig_stream = OpenAIProvider.chat_stream

    async def chat(self, _self: OpenAIProvider, *, messages: list[dict], tools: list[dict] | None) -> ProviderResponse:
        self.calls += 1
        text_blob = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))

        has_select = False
        select_outcomes: list[str] = []
        if tools:
            for t in tools:
                fn = t.get("function", {})
                if fn.get("name") == "select_outcome":
                    has_select = True
                    props = fn.get("parameters", {}).get("properties", {})
                    select_outcomes = props.get("outcome", {}).get("enum", [])
                    break

        if has_select and "handoff" in select_outcomes and "入口" in text_blob:
            return ProviderResponse(
                ok=True,
                tool_calls=[ToolCall(id=f"mock-{self.calls}", name="select_outcome", arguments={"outcome": "handoff", "instruction": "请继续处理"})],
            )

        if "执行节点" in text_blob or "测试执行" in text_blob:
            return ProviderResponse(ok=True, text="执行完成：已得到结果。")

        return ProviderResponse(ok=True, text="完成。")

    async def chat_stream(self, _self: OpenAIProvider, *, messages, tools, on_text, on_thinking) -> ProviderResponse:
        return await self.chat(_self, messages=messages, tools=tools)

    def install(self) -> None:
        mock = self

        async def _patched(provider_self, *, messages, tools):
            return await mock.chat(provider_self, messages=messages, tools=tools)

        async def _patched_stream(provider_self, *, messages, tools, on_text, on_thinking):
            return await mock.chat_stream(provider_self, messages=messages, tools=tools, on_text=on_text, on_thinking=on_thinking)

        OpenAIProvider.chat = _patched
        OpenAIProvider.chat_stream = _patched_stream

    def uninstall(self) -> None:
        OpenAIProvider.chat = self._orig_chat
        OpenAIProvider.chat_stream = self._orig_stream


def test_graph_loading() -> None:
    print("[test] graph loading...")
    wf = load_workflow(WORKSPACE, "bootstrap.default_chat")
    assert wf is not None
    assert wf.entry_node == "bootstrap.shell_orchestrator"
    assert next_node(wf, "bootstrap.executor", "completed") == "$end"
    print("  default_chat OK")


def test_node_loading() -> None:
    print("[test] node loading...")
    for nid in ["bootstrap.shell_orchestrator", "bootstrap.executor", "bootstrap.planner", "bootstrap.reviewer"]:
        n = load_node(WORKSPACE, nid)
        assert n is not None
        assert n.type == "ai"
        assert n.prompt.pack
        assert n.prompt.assembly
        assert n.model_route
        print(f"  {nid}: model_route={n.model_route}")


def test_prompt_assembly() -> None:
    print("[test] prompt assembly...")
    node = load_node(WORKSPACE, "bootstrap.executor")
    assert node is not None
    prompt = assemble_prompt(WORKSPACE, node)
    assert len(prompt) > 50
    print(f"  executor prompt: {len(prompt)} chars")


def test_outcomes() -> None:
    print("[test] allowed_outcomes...")
    wf = load_workflow(WORKSPACE, "bootstrap.default_chat")
    assert wf is not None
    oc = allowed_outcomes(wf, "bootstrap.shell_orchestrator")
    assert "reply" in oc
    assert "handoff" in oc
    print(f"  orchestrator outcomes: {oc}")


async def test_ai_node_execution() -> None:
    print("[test] AI node execution (mock)...")
    import httpx

    from engine.ai_step import run_ai_node
    from engine.context import RunContext
    from toolbox.registry import ToolRegistry

    mock = MockProvider()
    mock.install()
    try:
        wf = load_workflow(WORKSPACE, "bootstrap.default_chat")
        assert wf is not None
        node = load_node(WORKSPACE, "bootstrap.executor")
        assert node is not None
        registry = ToolRegistry(workspace_root=WORKSPACE, tools_dir=WORKSPACE / "tools")

        async with httpx.AsyncClient(trust_env=False) as http:
            rctx = RunContext(
                workspace_root=WORKSPACE,
                supervisor_url="http://127.0.0.1:9999",
                session_id="test-session",
                worker_id="test-worker",
                http=http,
                llm_http=http,
                api_key="mock-key",
                default_model="mock-model",
                task_id="task-executor",
            )
            provider = OpenAIProvider(http=http, api_key="mock-key", base_url="https://example.invalid", model="mock")
            outcome = await run_ai_node(
                rctx=rctx,
                provider=provider,
                registry=registry,
                workflow=wf,
                node=node,
                instruction="测试执行",
                history=[],
            )
            assert outcome.status == "completed"
            assert outcome.outcome == "completed"
            assert outcome.text
            print(f"  executor outcome={outcome.outcome}, text={outcome.text[:80]}")

        node2 = load_node(WORKSPACE, "bootstrap.shell_orchestrator")
        assert node2 is not None
        async with httpx.AsyncClient(trust_env=False) as http:
            rctx2 = RunContext(
                workspace_root=WORKSPACE,
                supervisor_url="http://127.0.0.1:9999",
                session_id="test-session",
                worker_id="test-worker",
                http=http,
                llm_http=http,
                api_key="mock-key",
                default_model="mock-model",
                task_id="task-orchestrator",
            )
            provider2 = OpenAIProvider(http=http, api_key="mock-key", base_url="https://example.invalid", model="mock")
            outcome2 = await run_ai_node(
                rctx=rctx2,
                provider=provider2,
                registry=registry,
                workflow=wf,
                node=node2,
                instruction="你好",
                history=[],
            )
            assert outcome2.status == "completed"
            assert outcome2.outcome == "handoff"
            print(f"  orchestrator outcome={outcome2.outcome}, instruction={outcome2.instruction or outcome2.text}")
    finally:
        mock.uninstall()


def main() -> None:
    print("=" * 60)
    print("Clonoth Engine Verification")
    print("=" * 60)
    test_graph_loading()
    test_node_loading()
    test_prompt_assembly()
    test_outcomes()
    asyncio.run(test_ai_node_execution())
    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
