from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskResult:
    """单个 task 片段的标准执行结果。"""

    node_id: str = ""
    kind: str = "final"  # final | yield_tool
    status: str = "completed"  # completed | failed | cancelled
    result_type: str = "outcome"  # outcome | yield_tool
    outcome: str = "completed"
    text: str = ""
    instruction: str = ""
    summary: str = ""
    context_ref: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "status": self.status,
            "result_type": self.result_type,
            "outcome": self.outcome,
            "text": self.text,
            "instruction": self.instruction,
            "summary": self.summary,
            "context_ref": self.context_ref,
            "tool_calls": list(self.tool_calls),
        }


NodeOutcome = TaskResult
