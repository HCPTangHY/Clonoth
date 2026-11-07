"""Request-only Gemini compatibility tails; no live API or code execution."""
from copy import deepcopy
import json

import httpx
import pytest

from providers import GeminiAIStudioProvider, GeminiVertexProvider


CHANNELS = (GeminiAIStudioProvider, GeminiVertexProvider)
THOUGHT_TAIL = {"role": "user", "parts": [{"thought": True, "text": " "}]}
SYSTEM_TAIL = {"role": "system", "parts": [{"text": " "}]}
EXECUTION_TAIL = {
    "role": "model",
    "parts": [{"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": " "}}],
}


def provider(cls, http=None):
    return cls(http=http, api_key="test-key", base_url="https://example.invalid", model="gemini-test")


@pytest.mark.parametrize("cls", CHANNELS)
@pytest.mark.parametrize("strip_system_role", (False, True))
@pytest.mark.parametrize("strip_thoughts", (False, True))
@pytest.mark.parametrize("ending", ("empty", "user", "assistant", "tool", "developer"))
def test_final_target_tail_conditions_and_input_isolation(cls, strip_system_role, strip_thoughts, ending):
    messages = [{"role": "system", "content": "static"}]
    if ending != "empty":
        messages.append({"role": "user", "content": "question"})
    if ending in ("assistant", "tool"):
        parts = [{"text": "answer", "thoughtSignature": "original-signature"}]
        message = {
            "role": "assistant", "content": "answer",
            "_meta": {"metadata": {cls.provider_name: {"raw_parts": parts}}, "tool_call_ids": []},
        }
        if ending == "tool":
            parts.append({"functionCall": {"name": "lookup", "args": {}}, "thoughtSignature": "call-signature"})
            message["tool_calls"] = [{"id": "real-call", "name": "lookup", "arguments": {}}]
            message["_meta"]["tool_call_ids"] = ["real-call"]
        messages.append(message)
    if ending == "tool":
        messages.append({"role": "tool", "tool_call_id": "real-call", "content": "real result"})
    if ending == "developer":
        messages.append({"role": "developer", "content": "later instruction"})
    original = deepcopy(messages)
    p = provider(cls)
    prepared = p.prepare_history(messages)
    _, converted = p._convert_messages(prepared, strip_thoughts=strip_thoughts)
    assert messages == original
    if ending == "assistant":
        assert converted[-1]["role"] == "model", "converter must not hide the actual tail from channel policy"
    if ending in ("empty", "assistant"):
        tail = [THOUGHT_TAIL]
    elif cls is GeminiAIStudioProvider:
        tail = [EXECUTION_TAIL, THOUGHT_TAIL]
    elif strip_system_role:
        tail = []
    else:
        tail = [SYSTEM_TAIL]
    kwargs = {"strip_system_role": strip_system_role, "strip_thoughts": strip_thoughts}
    body = p.build_payload(messages, **kwargs)
    assert body["contents"] == converted + tail
    assert body["systemInstruction"] == {"parts": [{"text": "static"}]}
    assert p.build_payload(messages, **kwargs) == body, "repeated requests must not accumulate tails"
    assert p.prepare_history(messages) == prepared, "tails never become replay history"
    body["contents"][-1]["parts"][0]["test_mutation"] = True
    assert messages == original
    assert p.prepare_history(messages) == prepared


@pytest.mark.parametrize("stream", (False, True))
def test_vertex_system_role_retry_removes_only_system_tail(stream):
    import asyncio

    async def run():
        requests = []
        messages = [{"role": "user", "content": "question"}]
        original = deepcopy(messages)

        def handle(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(400, json={"error": {"message": "role 'system' is not supported"}})
            reply = {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}
            if stream:
                return httpx.Response(200, text="data: " + json.dumps(reply) + "\n\n", headers={"Content-Type": "text/event-stream"})
            return httpx.Response(200, json=reply)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            p = provider(GeminiVertexProvider, http)
            result = await (p.chat_stream(messages=messages) if stream else p.chat(messages=messages))
        assert result.ok, result.error
        assert len(requests) == 2
        assert requests[0]["contents"] == requests[1]["contents"] + [SYSTEM_TAIL]
        assert "codeExecutionResult" not in json.dumps(requests)
        assert messages == original

    asyncio.run(run())
