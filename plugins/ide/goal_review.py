"""Deterministic review input: append-only outputs followed by one full task trace."""
from __future__ import annotations

import copy
import json
from pathlib import PurePosixPath
from typing import Any

from clonoth_runtime import get_int
from engine.compact import estimate_message_tokens
from engine.conversation_store import is_dynamic_context


async def review_cache_blocks(ctx: Any) -> Any:
    """Anthropic caches block boundaries, not prefixes inside one changing block.

    Keep one block per frozen output, with explicit points at the previous and
    current stable boundaries (at most two, plus provider system/automatic points).
    Other providers retain the original byte-identical JSON instruction.
    """
    from providers.anthropic import AnthropicProvider
    from engine.hooks.types import HookResult

    if (getattr(ctx.node, "id", "") != "ide.goal_reviewer"
            or not getattr(ctx.rctx, "is_system_task", False)
            or not isinstance(ctx.provider, AnthropicProvider)):
        return None
    cache = ctx.provider._options.get("cache_control", {"type": "ephemeral", "ttl": "1h"})
    if isinstance(cache, str) and cache.strip():
        cache = {"type": "ephemeral", **({"ttl": cache.strip()} if cache.strip() in {"1h", "5m"} else {})}
    if not isinstance(cache, dict) or not cache:
        return None
    for message in ctx.messages:
        text = message.get("content")
        if message.get("role") != "user" or not isinstance(text, str) or is_dynamic_context(message):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != 2 or "completed_outputs" not in payload or "current" not in payload:
            continue
        header = {key: payload[key] for key in ("schema_version", "goal_identity", "goal", "criteria")}
        segments = [encode(header)[:-1] + ',"completed_outputs":[']
        segments.extend(("," if index else "") + encode(row) for index, row in enumerate(payload["completed_outputs"]))
        segments.append('],"current":' + encode(payload["current"]) + "}")
        if "".join(segments) != text:
            continue  # Never rewrite an unexpected input representation.
        blocks = [{"type": "text", "text": segment} for segment in segments]
        count = len(payload["completed_outputs"])
        previous = min(count, max(0, int(payload["current"].get("previous_output_count", 0))))
        for index in {previous, count}:
            blocks[index]["cache_control"] = copy.deepcopy(cache)
        message["content"] = blocks
        return HookResult(modified=True)
    return None


class ReviewInputError(ValueError):
    """The harness cannot provide a complete review, not a failed work verdict."""


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def task_trace(evidence: list[dict], task_id: str) -> list[dict]:
    """Remove storage/provider duplication, never select tools by their meaning."""
    trace = []
    used_artifacts = set()
    artifacts = {row["artifact"]: row for row in evidence if row.get("source_task_id") == task_id and row.get("artifact")}
    for row in evidence:
        if row.get("source_task_id") != task_id or is_dynamic_context(row):
            continue
        if row.get("artifact"):
            trace.append({key: copy.deepcopy(row[key]) for key in ("artifact", "content", "truncated") if key in row})
            continue
        item = {key: copy.deepcopy(row[key]) for key in ("id", "role", "content", "tool_call_id", "name", "message_type") if key in row}
        if row.get("tool_calls"):
            calls = []
            for call in row["tool_calls"]:
                function = call.get("function") or call
                calls.append({"id": call.get("id", ""), "name": function.get("name", ""),
                              "arguments": copy.deepcopy(function.get("arguments", {}))})
            item["tool_calls"] = calls
        meta = row.get("meta") or row.get("_meta") or {}
        if meta.get("tool_result_truncated"):
            ref = str(meta.get("tool_result_ref") or "")
            artifact = artifacts.get(PurePosixPath(ref).name) if ref else None
            if not artifact or artifact.get("truncated"):
                raise ReviewInputError("主任务工具返回只有预览，完整 artifact 缺失，已暂停评审。")
            item["content"] = artifact["content"]
            item["artifact"] = artifact["artifact"]
            used_artifacts.add(artifact["artifact"])
        attachments = meta.get("attachments") or meta.get("tool_result_attachments")
        if attachments:
            item["attachments"] = copy.deepcopy(attachments)
        if item:
            trace.append(item)
    # Check structural identities only; do not guess which operation is a test or
    # whether a result is semantically successful. Missing pairs cannot be called full.
    pending: set[str] = set()
    for row in trace:
        for call in row.get("tool_calls", []):
            cid = call.get("id")
            if cid:
                if cid in pending:
                    raise ReviewInputError("主任务工具调用身份重复，无法确认完整过程。")
                pending.add(cid)
        if row.get("role") == "tool" and row.get("tool_call_id"):
            cid = row["tool_call_id"]
            if cid not in pending:
                raise ReviewInputError("主任务工具结果缺少对应调用，已暂停评审。")
            pending.remove(cid)
    if pending:
        raise ReviewInputError("主任务工具调用缺少完整返回，已暂停评审，不会截断后续轮。")
    return [row for row in trace if row.get("role") or row.get("artifact") not in used_artifacts]


def build_input(data: dict, snap: dict, node: Any, *, runtime_cfg: dict | None = None) -> tuple[str, dict]:
    """Freeze old reply/finish records; the changing candidate and trace stay last.

    The returned JSON has a stable key order. Adding to completed_outputs changes
    only the old array's closing delimiter and suffix, not any committed record.
    """
    candidate = data["_candidate"]
    latest = str(candidate.get("entry_task_id") or "")
    if not latest:
        raise ReviewInputError("缺少本轮主任务身份，已暂停评审。")
    tasks = snap.get("tasks") or []
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    task_ids = {row["task_id"] for row in tasks}
    if latest not in task_ids:
        raise ReviewInputError("本轮主任务不在 Goal 执行记录中，已暂停评审。")
    if latest in set(snap.get("truncated_task_ids", [])) or (
        "truncated_task_ids" not in snap and snap.get("evidence_truncated")
    ):
        raise ReviewInputError("本轮主任务完整过程未被保存（证据存储超限或失败），已暂停评审，不会截断后续轮。")
    if any((row.get("result") or {}).get("checkpoint_error") for row in tasks):
        raise ReviewInputError("相关任务消息或回复保存未确认，已暂停评审。")
    if set(snap.get("output_truncated_task_ids", [])).intersection(task_ids):
        raise ReviewInputError("相关任务的 reply/finish 记录不完整，已暂停评审。")

    # Older saved Goals gain this private per-version ledger lazily. Goal edits
    # clear it; reload, pause and planned restart preserve its exact bytes/order.
    committed = data.setdefault("_review_outputs", [])
    by_id = {row["id"]: row for row in committed}
    for raw in sorted(snap.get("outputs", []), key=lambda row: (row.get("sequence", 0), row["id"])):
        if raw.get("task_id") == latest or raw.get("task_id") not in task_ids:
            continue
        if raw.get("kind") not in {"reply", "finish"}:
            continue
        output = {key: copy.deepcopy(raw[key]) for key in ("id", "task_id", "node_id", "kind", "text", "attachments") if key in raw}
        previous = by_id.get(output["id"])
        if previous is not None:
            if previous != output:
                raise ReviewInputError("已保存的 reply/finish 内容发生变化，已暂停评审以保留原记录。")
            continue
        committed.append(output)
        by_id[output["id"]] = output

    trace = task_trace(snap.get("evidence", []), latest)
    if not trace or snap.get("evidence_missing"):
        raise ReviewInputError("缺少本轮主任务完整过程，已暂停评审；不会把旧任务回复当作本轮证据。")
    current_task = next(row for row in tasks if row["task_id"] == latest)
    result = current_task.get("result") or {}
    if result.get("checkpoint_error") or result.get("truncated"):
        raise ReviewInputError("本轮主任务的执行记录未完整确认，已暂停评审。")
    # Rules are supplied by the fixed reviewer system prompt. No live timestamps,
    # review counters, candidate text or state revisions precede this stable prefix.
    payload = {
        "schema_version": 2,
        "goal_identity": {"session_id": data["session_id"], "goal_id": data["_identity"], "goal_version": data["goal_version"]},
        "goal": data["goal"], "criteria": data["criteria"],
        "completed_outputs": copy.deepcopy(committed),
        "current": {
            "round": data["rounds"],
            "previous_output_count": len(data["reviews"][-1].get("output_ids", [])) if data["reviews"] else 0,
            "previous_review": data["reviews"][-1].get("verdict") if data["reviews"] else None,
            "prior_interruptions": data.get("_interruptions", []),
            "candidate": candidate.get("result", {}),
            "latest_main_task": {"task_id": latest, "node_id": current_task.get("node_id", ""),
                                 "status": current_task.get("status", ""), "trace": trace,
                                 "finish": copy.deepcopy(result.get("result", {})) if result.get("action") == "finish" else None},
        },
    }
    instruction = encode(payload)
    options = getattr(node, "extra", {}) if node else {}
    context_limit = options.get("context_limit")
    limit_source = "node"
    if context_limit is None:
        # Same configured budget and fallback as the main inference loop, not a model hard limit.
        context_limit = get_int(runtime_cfg or {}, "engine.compact.threshold_tokens", 100_000, min_value=0)
        limit_source = "runtime"
    if type(context_limit) is not int or context_limit < 0 or 0 < context_limit <= 2048:
        raise ReviewInputError("评审节点 context_limit 必须为 0（不限制）或大于 2048 的 token 预算。")
    prompt = getattr(node, "prompt", "") if node else ""
    prompt_text = prompt if isinstance(prompt, str) else encode(prompt)
    # ponytail: reuse the engine estimator, not a tokenizer or a session-usage delta.
    # Count this independent request; provider-specific token counting can replace the estimate.
    estimated_input = sum(estimate_message_tokens({"role": role, "content": text})
                          for role, text in (("system", prompt_text), ("user", instruction)))
    reserved = 2048
    required = estimated_input + reserved
    if context_limit and required > context_limit:
        raise ReviewInputError(
            f"完整评审输入超过 context_limit：估算输入 {estimated_input} token，"
            f"加预留 {reserved} 后需要约 {required}，配置预算为 {context_limit} token。"
            "这是估算，不是模型实际计数。已暂停，未截断、未发起评审或自动续轮；"
            "请调整评审或全局上下文预算后明确恢复。"
        )
    return instruction, {"input_bytes": len(instruction.encode("utf-8")), "context_limit": context_limit,
                         "context_limit_source": limit_source, "token_count_source": "estimate",
                         "estimated_input_tokens": estimated_input, "reserved_tokens": reserved,
                         "estimated_total_tokens": required, "latest_main_task_id": latest,
                         "output_ids": [row["id"] for row in committed]}
