"""Host-only response metadata contracts; all IO is local or mocked.

Run: python3.11 -B -m unittest discover -s tests -p 'test_adapter_response_info.py' -v
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.context import RunContext
from engine.conversation_store import ConversationStore, Message
from engine.hooks.registry import HookRegistry
from engine.hooks.types import HookContext, HookResult
from engine.inference import ai_step
from engine.protocol import TaskAction
from engine.runner import _message_to_history_dict
from providers.base import ProviderResponse, ToolCall
from supervisor.session import SessionMixin
from supervisor.task_router import TaskRouterMixin
from supervisor.types import HealthOut


INFO = {
    "provider": "actual-provider", "model": "actual-model",
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    "notices": [{"code": "fallback_provider.used", "label": "备用模型", "text": "HOST_ONLY_NOTICE"}],
}


def response_info():
    return copy.deepcopy(INFO)


def history_state(root):
    return SimpleNamespace(
        workspace_root=root, _lock=threading.RLock(),
        _entry_branch_ids_for_parent_locked=lambda session_id: set(),
    )


def task(task_id="task", info=None):
    return SimpleNamespace(
        task_id=task_id, node_id=task_id + "-node", session_id="session",
        result={"llm_request_id": task_id + "-request", "response_info": info or {}},
        caller_task_id=None, input={}, source_inbound_seq=None,
    )


class StoredResponseInfoTests(unittest.TestCase):
    def test_storage_round_trip_and_history_are_separate_from_model_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ConversationStore(root / "data" / "conversations")
            message = Message(
                id="answer", role="assistant", content="Answer", response_info=response_info(),
                meta={"reasoning": "summary", "metadata": {"actual-provider": {"signature": "signed"}}},
            )
            store.append("session", message)
            store.invalidate_cache("session")
            restored = store.load("session")[0]
            self.assertEqual(restored.response_info, INFO)
            self.assertEqual(restored.content, "Answer")
            row = SessionMixin.session_history_structured(history_state(root), session_id="session")[0]
            self.assertEqual(row["response_info"], INFO)
            replay = _message_to_history_dict(restored)
            self.assertNotIn("response_info", replay)
            self.assertNotIn("HOST_ONLY_NOTICE", json.dumps(replay))
            self.assertEqual(replay["_meta"]["metadata"]["actual-provider"]["signature"], "signed")

    def test_legacy_and_invalid_metadata_do_not_discard_other_message_fields(self):
        for value in (None, False, "invalid", [1]):
            message = Message.from_dict({"id": "old", "role": "assistant", "content": "saved", "response_info": value})
            self.assertEqual(message.content, "saved")
            self.assertEqual(message.response_info, {})
            self.assertNotIn("response_info", message.to_dict())
        self.assertEqual(Message.from_dict({"content": "legacy"}).response_info, {})

    def test_message_update_and_fork_preserve_host_info(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConversationStore(Path(temporary))
            store.append("parent", Message(id="answer", role="assistant", content="old", response_info=response_info()))
            self.assertTrue(store.update_message("parent", "answer", {"role": "assistant", "content": "new"}))
            self.assertEqual(store.fork("parent", "child"), 1)
            store.invalidate_cache("child")
            self.assertEqual(store.load("child")[0].response_info, INFO)

    def test_shadow_write_only_attaches_info_to_assistant(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ConversationStore(Path(temporary))
            rctx = SimpleNamespace(
                conversation_store=store, response_info=response_info(), session_id="session", child_session_id="",
                task_id="task", first_shadow_message_id="", last_shadow_message_id="",
            )
            loop = SimpleNamespace(rctx=rctx, node=SimpleNamespace(id="node"), last_shadow_message_id="")
            rows = [
                {"role": "assistant", "content": "answer"},
                {"role": "tool", "content": "result", "tool_call_id": "call", "name": "lookup"},
                {"role": "assistant", "content": "temporary", "_ephemeral": True},
            ]
            original = copy.deepcopy(rows)
            for row in rows:
                ai_step._shadow_write(loop, row)
            saved = store.load("session")
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0].response_info, INFO)
            self.assertEqual(saved[1].response_info, {})
            self.assertEqual(saved[1].tool_call_id, "call")
            self.assertEqual(rows, original)

    def test_outbound_metadata_is_not_message_text(self):
        state = SimpleNamespace(
            _lock=threading.RLock(), sessions={"session": object()},
            _inbound_events={}, _inbound_routed={}, eventlog=Mock(),
        )
        state.eventlog.append.return_value = {"seq": 7}
        result = SessionMixin.append_outbound_message(
            state, session_id="session", text="answer", response_info=response_info(),
            node_id="producer", reply_node_id="owner", task_id="task", llm_request_id="request",
        )
        self.assertTrue(result["ok"])
        payload = state.eventlog.append.call_args.kwargs["payload"]
        self.assertEqual(payload["text"], "answer")
        self.assertEqual(payload["response_info"], INFO)
        self.assertEqual(payload["reply_node_id"], "owner")

    def test_output_chain_prefers_producer_and_preserves_entry_fallback_notice(self):
        source = task("source", response_info())
        producer = task("reviewer")
        state = SimpleNamespace(
            _route_session_id_for_task_locked=lambda task: "session", append_outbound_message=Mock(),
        )
        for producer_info in ({}, {"provider": "review-model", "notices": []}):
            producer.result["response_info"] = producer_info
            TaskRouterMixin._resume_caller_or_output_locked(
                state, source, {"result": {"text": "answer"}}, {"text": "answer"},
                action_type="finish", delivery_task=producer,
            )
            delivered = state.append_outbound_message.call_args.kwargs
            self.assertEqual(delivered["response_info"], producer_info or INFO)
            self.assertEqual(delivered["text"], "answer")
            self.assertEqual(delivered["node_id"], "reviewer-node")
            self.assertEqual(delivered["reply_node_id"], "source-node")
            self.assertEqual(delivered["llm_request_id"], "reviewer-request")
            self.assertNotIn("response_info", delivered["text"])

    def test_health_response_is_backward_compatible(self):
        fields = dict(run_id="run", started_at=datetime.now(timezone.utc), uptime_seconds=1)
        self.assertEqual(HealthOut(**fields).workspace_root, "")
        self.assertEqual(HealthOut(**fields, workspace_root="/fixture/workspace").workspace_root, "/fixture/workspace")


class RuntimeResponseInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_reset_and_event_filtering(self):
        requests = []

        def receive(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as http:
            rctx = RunContext(
                workspace_root=Path("/fixture"), supervisor_url="https://supervisor.example",
                session_id="branch", parent_session_id="parent", worker_id="worker", http=http, llm_http=http,
                task_id="task", source_inbound_seq=5,
            )
            first_request = rctx.begin_llm_request()
            rctx.response_info = response_info()
            with patch("engine.context.get_bus", return_value=Mock()):
                await rctx.emit_event("assistant_text", {"text": "answer"})
                await rctx.emit_event("tool_call_end", {"result": {"ok": True}})
                await rctx.emit_event("stream_delta", {"text": "delta"})
                rctx.begin_llm_request()
                await rctx.emit_event("assistant_text", {"text": "next"})
            self.assertNotEqual(first_request, rctx.current_llm_request_id)
            self.assertEqual(requests[0]["payload"]["response_info"], INFO)
            self.assertEqual(requests[1]["payload"]["response_info"], INFO)
            self.assertEqual(requests[0]["payload"]["branch_session_id"], "branch")
            self.assertEqual(requests[0]["payload"]["source_inbound_seq"], 5)
            self.assertNotIn("response_info", requests[2]["payload"])
            self.assertNotIn("response_info", requests[3]["payload"])
            self.assertEqual(rctx.response_info, {})

    async def test_notice_channel_first_defined_wins_without_touching_messages(self):
        def absent(ctx):
            return HookResult()

        def first(ctx):
            return HookResult(channels={"response_notices": INFO["notices"]})

        def second(ctx):
            return HookResult(channels={"response_notices": [{"text": "other"}]})

        registry = HookRegistry()
        registry.register("after_llm_call", absent, priority=30)
        registry.register("after_llm_call", first, priority=20)
        registry.register("after_llm_call", second, priority=10)
        messages = [{"role": "user", "content": "question"}]
        result = await registry.afire("after_llm_call", HookContext(messages=messages, tools=[], node=None, provider=None, rctx=None))
        self.assertEqual(result.channels["response_notices"], INFO["notices"])
        self.assertEqual(messages, [{"role": "user", "content": "question"}])

    async def test_inference_records_accepted_provider_not_configured_primary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = SimpleNamespace(name="primary", model="primary-model")
            accepted = SimpleNamespace(name="actual-provider", model="actual-model")
            rctx = SimpleNamespace(workspace_root=root, task_context={}, session_id="session", completed_steps=0, response_info={})
            node = SimpleNamespace(id="node", tool_mode="native")
            response = ProviderResponse(ok=True, tool_calls=[ToolCall(id="call", name="finish", arguments={})], usage=INFO["usage"])
            loop = SimpleNamespace(
                rctx=rctx, node=node, provider=primary, messages=[], openai_tools=[],
                use_stream=False, last_usage={}, last_prompt_tokens=0,
            )
            action = TaskAction(action="finish", node_id="node", result={"text": "answer"})

            async def fire(point, context):
                if point == "after_llm_call":
                    context.provider = accepted
                    context.response = response
                    return HookResult(channels={"response_notices": INFO["notices"]})
                return HookResult()

            with patch.object(ai_step, "auto_discover_and_register"), \
                 patch.object(ai_step, "load_external_plugins"), \
                 patch.object(ai_step, "load_runtime_config", return_value={"engine": {"max_steps": 1}}), \
                 patch.object(ai_step, "assemble_initial_messages", return_value=([], False, [])), \
                 patch.object(ai_step, "build_node_tool_specs", return_value=(set(), [])), \
                 patch.object(ai_step, "allowed_pseudo_names", return_value=set()), \
                 patch.object(ai_step, "_build_loop_state", return_value=loop), \
                 patch.object(ai_step, "_call_llm_with_retry", new=AsyncMock(return_value=ProviderResponse(ok=False, error="primary failed"))), \
                 patch.object(ai_step.hook_registry, "afire", side_effect=fire), \
                 patch.object(ai_step, "_handle_tool_calls", new=AsyncMock(return_value=action)), \
                 patch.object(ai_step, "_fire_task_end_hook_if_finish", new=AsyncMock(return_value=action)):
                result = await ai_step.run_ai_node(rctx=rctx, provider=primary, registry=Mock(), node=node, instruction="q", history=[])
            self.assertIs(result, action)
            self.assertEqual(rctx.response_info, INFO)
            self.assertIs(loop.response_provider, accepted)
            self.assertIs(loop.provider, primary)
            self.assertNotIn("response_info", result.result)

    async def test_retry_fallback_and_cancel_fail_notices_persist_for_history_without_polluting_llm(self):
        from engine.builtin import fallback_provider
        from engine.conversation_store import ConversationStore, Message
        from engine.inference import llm_call
        from engine.inference.loop_state import _LoopState
        from engine.node import Node
        from supervisor.eventlog import EventLog
        from supervisor.state import SupervisorState

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config" / "nodes").mkdir(parents=True)
            (root / "config" / "nodes" / "main.yaml").write_text("id: main\ntype: ai\noutput_mode: hybrid\n", encoding="utf-8")

            # 1. Verify _call_llm_with_retry records retry reasons in rctx.step_notices
            attempts = [
                ProviderResponse(ok=False, error="502 Bad Gateway upstream", status_code=502),
                ProviderResponse(ok=True, text="recovered answer", usage={"prompt_tokens": 10, "completion_tokens": 5}),
            ]
            idx = {"i": 0}

            class FlakyProvider:
                name = "pri"
                model = "pri-model"

                async def chat(self, **kwargs):
                    res = attempts[idx["i"]]
                    idx["i"] += 1
                    return res

            rctx = SimpleNamespace(
                workspace_root=root, task_id="t1", session_id="s1",
                begin_llm_request=lambda: "req-1", emit_event=AsyncMock(),
                check_cancelled=AsyncMock(return_value=False),
                check_preempted=AsyncMock(return_value={}),
                response_info={}, step_notices=[], llm_http=None,
            )
            ls = _LoopState(
                rctx=rctx, node=Node(id="main", type="ai"), provider=FlakyProvider(),
                registry=None, run_id="r", context_ref="", runtime_cfg={},
                streaming=False, use_stream=False, messages=[{"role": "user", "content": "hi"}],
                system_prompt=[], is_block_mode=False, openai_tools=[], history=[],
                collected_attachments=[], tool_produced_attachments=[],
                retry_max=3, retry_initial_delay=0.01, retry_max_delay=0.02, retry_backoff=1.0,
            )
            with patch.object(llm_call.hook_registry, "afire", new=AsyncMock(return_value=HookResult())):
                resp = await llm_call._call_llm_with_retry(ls, 0)
            self.assertTrue(resp.ok)
            self.assertEqual(len(rctx.step_notices), 1)
            self.assertEqual(rctx.step_notices[0]["code"], "llm.retry")
            self.assertIn("502 Bad Gateway upstream", rctx.step_notices[0]["text"])

            # 2. Verify fallback_provider includes original error reason in response_notices
            fb_prov = SimpleNamespace(name="backup", model="backup-model")
            ctx = HookContext(
                messages=ls.messages, tools=[], node=ls.node, provider=ls.provider, rctx=rctx,
                response=ProviderResponse(ok=False, error="429 rate limited", status_code=429),
                extra={"loop_state": ls, "call_provider": AsyncMock(return_value=ProviderResponse(ok=True, text="fb ok"))},
            )
            with patch.object(fallback_provider, "_load_config", return_value={"fallbacks": [{"provider": "openai", "base_url": "https://fb.example", "api_key": "secret-k", "model": "backup-model"}]}), \
                 patch.object(fallback_provider, "_create_fallback_provider", return_value=fb_prov):
                fb_res = await fallback_provider.FallbackProviderHandler().handle(ctx)
            fb_notice = fb_res.channels["response_notices"][0]
            self.assertIn("429 rate limited", fb_notice["text"])
            self.assertNotIn("secret-k", fb_notice["text"])

            # 3. Verify Supervisor persists cancelled & error notices to JSONL and exports response_info even with empty meta
            with patch("supervisor.state.auto_discover_and_register", return_value={}), \
                 patch("engine.hooks.loader.load_external_plugins", return_value=0):
                state = SupervisorState(workspace_root=root, eventlog=EventLog(root / "data" / "events.jsonl", run_id="b1"), policy=SimpleNamespace())
            state._default_entry_node = lambda: "main"
            state._maybe_trigger_turn_summary_locked = lambda task: None
            sid = state.get_or_create_session(channel="web", conversation_key="web:hist")
            store = ConversationStore(root / "data" / "conversations")
            store.append(sid, Message(
                id="a-empty-meta", role="assistant", content="ok", meta={},
                response_info={"provider": "backup", "model": "backup-model", "notices": [*rctx.step_notices, fb_notice]},
            ))

            # Manual cancel on running entry task
            inb = state.execution_runtime.submit_input(sid, "stop me", "req-stop-1")
            tid = inb["task_id"]
            claimed = state.assign_next_task(worker_id="w1")
            state.cancel_single_task(tid)
            state.complete_task(task_id=tid, worker_id="w1", execution_id=claimed["execution_id"], result={"action": "cancelled", "error": "task cancelled"})

            # Orphan cancel merges branch and writes restart interruption notice
            inb2 = state.execution_runtime.submit_input(sid, "restart me", "req-restart-2")
            tid2 = inb2["task_id"]
            state.assign_next_task(worker_id="w1")
            branch_sid = state.tasks[tid2].session_id
            store.append(branch_sid, Message(id="partial-tool", role="assistant", content="working...", source_task_id=tid2))
            state.cancel_orphaned_tasks()

            structured = state.session_history_structured(session_id=sid)
            self.assertEqual(structured[0]["response_info"]["notices"][0]["code"], "llm.retry")
            self.assertEqual(structured[0]["response_info"]["notices"][1]["code"], "fallback_provider.used")
            cancel_rows = [m for m in structured if m.get("message_type") == "cancelled"]
            self.assertEqual(len(cancel_rows), 2)
            self.assertIn("手动取消", cancel_rows[0]["content"])
            self.assertIn("服务重启中断", cancel_rows[1]["content"])
            self.assertTrue(any(m.get("id") == "partial-tool" for m in structured))


if __name__ == "__main__":
    unittest.main()
