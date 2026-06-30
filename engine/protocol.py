from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
#  v3 统一动作协议
# ---------------------------------------------------------------------------

ACTION_DISPATCH = "dispatch"
ACTION_FINISH = "finish"
# [AutoC 2026-05-31] Why: ask is a terminal pseudo-tool distinct from finish so
# later topology routing can tell clarification requests from final deliveries.
# How: add a first-class action constant while Phase 0 routes it like finish.
# Purpose: avoid stringly typed ask handling across engine and supervisor code.
ACTION_ASK = "ask"
ACTION_FAIL = "fail"
ACTION_CANCELLED = "cancelled"
ACTION_PREEMPTED = "preempted"
# [AutoC 2026-06-10] Why: output chain quality gates need to reject an upstream
# finish without pretending to ask for user clarification. How: add a terminal
# action name that serializes like finish/ask. Purpose: downstream chain nodes
# can explicitly send the entry node back for revision.
ACTION_REJECT = "reject"


@dataclass
class TaskAction:
    """节点执行后返回的统一动作。

    所有节点（AI 或 Tool）执行完毕后，只能返回以下动作之一：
      - dispatch:   委派给另一个节点（AI 或 Tool）
      - finish:     任务完成，把结果交回去（Supervisor 根据 caller 决定交给谁）
      - ask:        请求上游补充信息；Phase 0 暂按 finish 路由
      - reject:     拒绝上游输出（仅用于 output chain 下游节点打回上游）
      - fail:       执行失败
      - cancelled:  被取消
      - preempted:  被软打断（上下文已保存）
    """

    action: str  # dispatch | finish | ask | fail | cancelled | preempted | reject
    node_id: str = ""  # 产出此动作的节点 id

    # dispatch 时填写
    target_node: str = ""  # 委派目标节点 id
    dispatch_input: dict[str, Any] = field(default_factory=dict)
    dispatch_batch: list[dict[str, Any]] = field(default_factory=list)  # 批量委派 [{kind, target, instruction/arguments, ...}]

    # finish 时填写
    result: dict[str, Any] = field(default_factory=dict)

    # fail 时填写
    error: str = ""

    # 通用
    context_ref: str = ""  # 当前节点的上下文快照引用
    # Child Session 隔离（Phase B）：子节点使用的 child session ID。
    # 非空时表示此 task 的消息存储在 child session 的 JSONL 中，而非 snapshot。
    child_session_id: str = ""
    summary: str = ""  # 简短摘要（用于事件日志、进度展示）
    # [AutoC 2026-06-04] Why: supervisor emits outbound_message after the engine
    # task returns, but the frontend must replace the card for the specific LLM
    # request that produced finish/ask. How: carry that request id through the task
    # result protocol. Purpose: final delivery is request-scoped instead of task-scoped.
    llm_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action,
            "node_id": self.node_id,
            "context_ref": self.context_ref,
            "summary": self.summary,
        }
        if self.llm_request_id:
            d["llm_request_id"] = self.llm_request_id
        # Child Session 隔离（Phase B）：将 child_session_id 写入 task result
        if self.child_session_id:
            d["child_session_id"] = self.child_session_id
        if self.action == ACTION_DISPATCH:
            d["target_node"] = self.target_node
            d["dispatch_input"] = dict(self.dispatch_input)
            if self.dispatch_batch:
                d["dispatch_batch"] = list(self.dispatch_batch)
        elif self.action in (ACTION_FINISH, ACTION_ASK, ACTION_REJECT):
            # [AutoC 2026-05-31] Why: ask carries the same result payload shape as
            # finish in Phase 0. How: serialize both actions through the same
            # branch. Purpose: supervisors can receive action="ask" without losing
            # text/summary fields.
            # [AutoC 2026-06-10] Why: reject returns the same text/summary payload
            # as finish and ask. How: include ACTION_REJECT in this serialization
            # branch. Purpose: supervisor receives the reject reason intact.
            d["result"] = dict(self.result)
        elif self.action == ACTION_FAIL:
            d["error"] = self.error
        return d
