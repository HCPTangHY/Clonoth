"""验证新引擎的图遍历和 AI 节点执行逻辑。

用法：python scripts/verify_engine.py

不需要真实 LLM 或 Supervisor，全部用 mock。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

os.environ.setdefault("OPENAI_API_KEY", "mock-key")
os.environ.setdefault("OPENAI_MODEL", "mock-model")

from providers.base import ProviderResponse, ToolCall
from providers.openai import OpenAIProvider

from engine.graph import load_workflow, next_node, allowed_outcomes
from engine.node import load_node
from engine.prompt import assemble_prompt
from engine.protocol import NodeOutcome


# ---- Mock OpenAI ----

class MockProvider:
    def __init__(self) -> None:
        self.calls = 0
        self._orig = OpenAIProvider.chat

    async def chat(self, _self: OpenAIProvider, *, messages: list[dict], tools: list[dict] | None) -> ProviderResponse:
        self.calls += 1
        text_blob = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))

        # 检查是否有 select_outcome 工具
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

        # 入口节点：如果有 select_outcome，选 handoff
        if has_select and "handoff" in select_outcomes and "入口" in text_blob and "下游节点执行结果" not in text_blob:
            return ProviderResponse(
                ok=True,
                tool_calls=[ToolCall(
                    id=f"mock-{self.calls}",
                    name="select_outcome",
                    arguments={"outcome": "handoff", "instruction": "请继续处理"},
                )],
            )

        # 入口节点收到下游结果后，生成最终回复
        if "入口" in text_blob and "下游节点执行结果" in text_blob:
            return ProviderResponse(ok=True, text="最终回复：综合下游结果，已完成处理。")

        # 回复节点：直接输出文本
        if "回复" in text_blob or "reply_responder" in text_blob:
            return ProviderResponse(ok=True, text="最终回复：已完成处理。")

        # planner
        if "kernel_planner" in text_blob or "规划" in text_blob:
            return ProviderResponse(ok=True, text="规划完成：先整理需求，再执行。")

        # reviewer
        if "kernel_reviewer" in text_blob or "复核" in text_blob:
            return ProviderResponse(ok=True, text="复核完成：结果正确。")

        # executor (default)
        return ProviderResponse(ok=True, text="执行完成：已得到结果。")

    def install(self) -> None:
        mock = self
        async def _patched(provider_self, *, messages, tools):
            return await mock.chat(provider_self, messages=messages, tools=tools)
        OpenAIProvider.chat = _patched

    def uninstall(self) -> None:
        OpenAIProvider.chat = self._orig


# ---- Tests ----

def test_graph_loading() -> None:
    print("[test] graph loading...")
    wf = load_workflow(WORKSPACE, "bootstrap.default_chat")
    assert wf is not None, "default_chat workflow not loaded"
    assert wf.entry_node == "bootstrap.shell_orchestrator"
    assert "bootstrap.shell_orchestrator" in wf.edges
    assert "bootstrap.executor" in wf.edges
    print(f"  default_chat: entry={wf.entry_node}, nodes={list(wf.edges.keys())}")

    wf2 = load_workflow(WORKSPACE, "bootstrap.plan_execute_review")
    assert wf2 is not None
    assert "bootstrap.planner" in wf2.edges
    assert "bootstrap.reviewer" in wf2.edges
    print(f"  plan_execute_review: entry={wf2.entry_node}, nodes={list(wf2.edges.keys())}")

    # next_node
    assert next_node(wf, "bootstrap.shell_orchestrator", "reply") == "$reply"
    assert next_node(wf, "bootstrap.shell_orchestrator", "handoff") == "bootstrap.executor"
    assert next_node(wf, "bootstrap.executor", "completed") == "bootstrap.shell_orchestrator"
    print("  graph traversal OK")


def test_node_loading() -> None:
    print("[test] node loading...")
    ids = [
        "bootstrap.shell_orchestrator",
        "bootstrap.executor",
        "bootstrap.planner",
        "bootstrap.reviewer",
    ]
    for nid in ids:
        n = load_node(WORKSPACE, nid)
        assert n is not None, f"node {nid} not loaded"
        assert n.type == "ai"
        assert n.prompt.pack, f"node {nid} missing prompt.pack"
        assert n.prompt.assembly, f"node {nid} missing prompt.assembly"
        assert n.model_route, f"node {nid} missing model_route"
        print(f"  {nid}: model_route={n.model_route}, tool_access={n.tool_access.mode}, output={n.output_mode}")


def test_prompt_assembly() -> None:
    print("[test] prompt assembly...")
    node = load_node(WORKSPACE, "bootstrap.executor")
    assert node is not None
    prompt = assemble_prompt(WORKSPACE, node)
    assert len(prompt) > 50, f"prompt too short ({len(prompt)} chars)"
    print(f"  executor prompt: {len(prompt)} chars")


def test_outcomes() -> None:
    print("[test] allowed_outcomes...")
    wf = load_workflow(WORKSPACE, "bootstrap.default_chat")
    assert wf is not None
    oc = allowed_outcomes(wf, "bootstrap.shell_orchestrator")
    assert "reply" in oc
    assert "handoff" in oc
    print(f"  orchestrator outcomes: {oc}")

    oc2 = allowed_outcomes(wf, "bootstrap.executor")
    assert "completed" in oc2
    print(f"  executor outcomes: {oc2}")


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
            assert outcome.node_id == "bootstrap.executor"
            assert outcome.outcome == "completed"
            assert outcome.text, "output text should not be empty"
            print(f"  executor outcome={outcome.outcome}, text={outcome.text[:80]}")


        async def _mock_on_handoff(outcome_name: str, instruction: str) -> str:
            return f"下游执行完成: outcome={outcome_name}, instruction={instruction}"

        # 测试入口节点 select_outcome
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
                on_handoff=_mock_on_handoff,
            )
            print(f"  orchestrator outcome={outcome2.outcome}, text={outcome2.text[:80]}")
            # 入口节点应该先 handoff，收到子链结果后，返回最终 reply 或 completed
            assert outcome2.outcome in {"reply", "completed"}, f"expected reply/completed, got {outcome2.outcome}"
            assert "最终回复" in outcome2.text or "完成" in outcome2.text, f"unexpected text: {outcome2.text}"
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
