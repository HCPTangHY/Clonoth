"""统一消息模型与元数据。

为消息和上下文快照提供结构化元数据，为后续跨 provider 思维链保留、
JSON tool mode 等功能做准备。

设计原则：
  - 不破坏现有代码：所有元数据字段可选，现有 dict 消息格式完全兼容
  - 渐进式采用：新代码可以开始写入 meta，旧代码忽略即可
  - 快照格式向后兼容：v1 快照无 meta 字段，加载时自动补空
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
#  消息级元数据
# ---------------------------------------------------------------------------

@dataclass
class MessageMeta:
    """单条消息的元数据，持久化到 context snapshot。

    这些信息不发送给 LLM，仅用于引擎内部追踪和跨 provider 兼容。

    [refactor 2026-04-18] 字段重命名与结构调整：
      - raw_parts → metadata：provider 自治命名空间 {provider_name: {...}}
      - thinking_text → reasoning：统一思维链字段命名
      - has_thinking → has_reasoning
      from_dict 向后兼容旧快照中的 raw_parts / thinking_text / has_thinking
    """
    # ── 通用字段 ──
    provider: str = ""              # 生成该消息的 provider (claude/gemini/openai)
    # [2026-05-01] 默认工具模式改为 fake-native。
    # 原因：历史上的 native 实际是文本化 fake-native；无标记旧消息必须按旧格式读取。
    tool_mode: str = "fake-native"  # fake-native / native / json — 该消息使用的工具调用格式
    message_type: str = ""          # user_input / tool_result / assistant / system
    timestamp: str = ""             # ISO 格式生成时间
    # [AutoC 2026-06-04] Why: structured history and outbound replacement need to
    # correlate one assistant row with one provider request. How: persist the current
    # request id in message metadata. Purpose: refreshed history can keep request-level
    # identity instead of only task-level identity.
    llm_request_id: str = ""

    # ── Provider 元数据命名空间 ──
    # [refactor 2026-04-18] 替代旧 raw_parts，结构为 {provider_name: {provider 自定义内容}}
    # engine 只搬运不解读，各 provider 自行决定存什么
    metadata: dict = field(default_factory=dict)

    tool_call_ids: list[str] = field(default_factory=list)
    # 原始 tool_call id 列表

    # [2026-05-07] 控制流工具标记。
    # 原因：finish 这类伪工具只驱动引擎控制流，不能像普通业务工具一样长期回放。
    # 做法：在 tool_result 等运行期消息的 meta 中记录控制工具名和状态。
    # 目的：存储层、快照层、L2 历史构造层可以一致跳过这类临时协议配对消息。
    control_tool_name: str = ""
    control_tool_status: str = ""
    # [2026-06-07] Structured tool rejection metadata.
    # Why: frontend must not parse localized rejection text to decide presentation.
    # How: persist hook rejection code and a presentation hint on tool_result rows.
    # Purpose: rejected mixed finish batches can be hidden by structure.
    tool_rejected: bool = False
    tool_rejection_code: str = ""
    tool_result_visibility: str = ""

    # ── 提取层（bot/前端可读）──
    # [refactor 2026-04-18] thinking_text → reasoning, has_thinking → has_reasoning
    reasoning: str = ""             # 思维链正文（给 debug/展示用）
    has_reasoning: bool = False     # 快速判断是否含思维链
    inline_data: list = field(default_factory=list)
    # 附件引用列表（图片等 inlineData 的 mime_type + 引用信息，不含原始 base64）

    usage: dict = field(default_factory=dict)
    # LLM 响应的 token usage，仅 assistant 消息有值
    # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}

    # [thinking-time 2026-06-01] Precise thinking timing for web frontend.
    # reasoning_started_at: first thinking token wall-clock ISO timestamp.
    # reasoning_ended_at: first text token (= thinking end) ISO timestamp.
    reasoning_started_at: str = ""
    reasoning_ended_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict。省略空值/默认值字段以节省空间。

        [refactor 2026-04-18] 使用新字段名 metadata/reasoning/has_reasoning 序列化。
        """
        d: dict[str, Any] = {}
        if self.provider:
            d["provider"] = self.provider
        # [2026-05-01] 只省略 fake-native 默认值；真 native 必须显式写入，
        # 目的：下一轮 build_llm_messages 能把真 native 历史路由到 NativeToolFormatter。
        if self.tool_mode and self.tool_mode != "fake-native":
            d["tool_mode"] = self.tool_mode
        if self.message_type:
            d["message_type"] = self.message_type
        if self.timestamp:
            d["timestamp"] = self.timestamp
        if self.llm_request_id:
            d["llm_request_id"] = self.llm_request_id
        if self.metadata:
            d["metadata"] = self.metadata
        if self.tool_call_ids:
            d["tool_call_ids"] = list(self.tool_call_ids)
        if self.control_tool_name:
            d["control_tool_name"] = self.control_tool_name
        if self.control_tool_status:
            d["control_tool_status"] = self.control_tool_status
        if self.tool_rejected:
            d["tool_rejected"] = True
        if self.tool_rejection_code:
            d["tool_rejection_code"] = self.tool_rejection_code
        if self.tool_result_visibility:
            d["tool_result_visibility"] = self.tool_result_visibility
        if self.reasoning:
            d["reasoning"] = self.reasoning
        if self.has_reasoning:
            d["has_reasoning"] = True
        if self.inline_data:
            d["inline_data"] = self.inline_data
        if self.usage:
            d["usage"] = self.usage
        if self.reasoning_started_at:
            d["reasoning_started_at"] = self.reasoning_started_at
        if self.reasoning_ended_at:
            d["reasoning_ended_at"] = self.reasoning_ended_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageMeta:
        """从 dict 反序列化。

        [refactor 2026-04-18] 向后兼容旧快照：
          - thinking_text → reasoning
          - has_thinking  → has_reasoning
          - raw_parts     → metadata["legacy"]（旧数据迁移）
        新快照优先读取新字段名。
        """
        if not isinstance(data, dict):
            return cls()

        # ── 向后兼容：metadata 优先新字段，回退 raw_parts → metadata.legacy ──
        metadata = data.get("metadata")
        if not metadata:
            _old_raw_parts = data.get("raw_parts")
            if _old_raw_parts:
                metadata = {"legacy": {"raw_parts": list(_old_raw_parts)}}
            else:
                metadata = {}

        # ── 向后兼容：reasoning 优先新字段，回退 thinking_text ──
        reasoning = str(data.get("reasoning") or data.get("thinking_text") or "")
        has_reasoning = bool(data.get("has_reasoning") or data.get("has_thinking"))

        return cls(
            provider=str(data.get("provider") or ""),
            tool_mode=str(data.get("tool_mode") or "fake-native"),
            message_type=str(data.get("message_type") or ""),
            timestamp=str(data.get("timestamp") or ""),
            llm_request_id=str(data.get("llm_request_id") or ""),
            metadata=dict(metadata),
            tool_call_ids=list(data.get("tool_call_ids") or []),
            control_tool_name=str(data.get("control_tool_name") or ""),
            control_tool_status=str(data.get("control_tool_status") or ""),
            tool_rejected=bool(data.get("tool_rejected")),
            tool_rejection_code=str(data.get("tool_rejection_code") or ""),
            tool_result_visibility=str(data.get("tool_result_visibility") or ""),
            reasoning=reasoning,
            has_reasoning=has_reasoning,
            inline_data=list(data.get("inline_data") or []),
            usage=dict(data.get("usage") or {}),
            reasoning_started_at=str(data.get("reasoning_started_at") or ""),
            reasoning_ended_at=str(data.get("reasoning_ended_at") or ""),
        )


# ---------------------------------------------------------------------------
#  快照级元数据
# ---------------------------------------------------------------------------

@dataclass
class SnapshotMeta:
    """上下文快照的全局元数据。

    存储于 snapshot["meta"] 字段中，与现有 v1 格式兼容（v1 无此字段）。
    """
    provider: str = ""           # 当前使用的 provider
    # [2026-05-01] 快照默认也改为 fake-native，避免旧快照被解释为真 native。
    tool_mode: str = "fake-native"  # 当前工具调用格式
    message_metas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # key = 消息索引（字符串），value = MessageMeta.to_dict()
    # 只记录有实际元数据的消息，空 meta 不占位

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的 dict。"""
        d: dict[str, Any] = {}
        if self.provider:
            d["provider"] = self.provider
        # [2026-05-01] 与 MessageMeta 保持一致：fake-native 是兼容默认值，
        # 真 native 和 json 都需要显式持久化。
        if self.tool_mode and self.tool_mode != "fake-native":
            d["tool_mode"] = self.tool_mode
        if self.message_metas:
            d["message_metas"] = dict(self.message_metas)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SnapshotMeta:
        """从 dict 反序列化。None 或非 dict 返回空实例。"""
        if not isinstance(data, dict):
            return cls()
        return cls(
            provider=str(data.get("provider") or ""),
            tool_mode=str(data.get("tool_mode") or "fake-native"),
            message_metas=dict(data.get("message_metas") or {}),
        )


# ---------------------------------------------------------------------------
#  辅助函数
# ---------------------------------------------------------------------------

def get_message_meta(message: dict[str, Any]) -> MessageMeta:
    """从消息 dict 中提取 _meta 字段，返回 MessageMeta 实例。

    如果消息没有 _meta 字段，返回空的 MessageMeta。
    """
    raw = message.get("_meta")
    if isinstance(raw, MessageMeta):
        return raw
    if isinstance(raw, dict):
        return MessageMeta.from_dict(raw)
    return MessageMeta()


def set_message_meta(message: dict[str, Any], meta: MessageMeta) -> None:
    """将 MessageMeta 写入消息 dict 的 _meta 字段。

    如果 meta 为空（所有字段都是默认值），不写入以保持消息干净。
    """
    d = meta.to_dict()
    if d:
        message["_meta"] = d
    elif "_meta" in message:
        del message["_meta"]


def strip_internal_fields(message: dict[str, Any]) -> dict[str, Any]:
    """剥离所有以 _ 开头的内部标记，返回可发送给 LLM 的干净消息。

    包括: _dynamic, _ephemeral, _meta 等。
    """
    return {k: v for k, v in message.items() if not k.startswith("_")}


def build_snapshot_dict(
    *,
    node_id: str,
    messages: list[dict[str, Any]],
    step_count: int,
    meta: SnapshotMeta | None = None,
) -> dict[str, Any]:
    """构建上下文快照 dict，兼容 v1 格式。

    如果提供了 meta 且非空，写入 snapshot["meta"] 字段（v2 扩展）。
    """
    snapshot: dict[str, Any] = {
        "version": 1,
        "node_id": node_id,
        "messages": messages,
        "step_count": int(step_count),
    }
    if meta is not None:
        meta_dict = meta.to_dict()
        if meta_dict:
            snapshot["meta"] = meta_dict
    return snapshot


def load_snapshot_meta(snapshot: dict[str, Any]) -> SnapshotMeta:
    """从快照 dict 中提取 SnapshotMeta。v1 快照无 meta 字段时返回空实例。"""
    return SnapshotMeta.from_dict(snapshot.get("meta"))
