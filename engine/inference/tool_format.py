"""工具格式抽象层。

提供 ToolFormatter ABC，封装工具定义注入、响应解析、结果格式化三个环节。
当前实现：
  - NativeToolFormatter:     真原生工具调用，保留 assistant.tool_calls 与 role=tool 结果
  - FakeNativeToolFormatter: 旧的 fake-native 文本历史兼容模式
  - JsonToolFormatter:       使用 JSON 文本块内嵌工具调用

设计目标：
  让 ai_step 的推理循环不直接依赖 OpenAI tool calling 格式，
  后续切换到 JSON 文本工具调用时只需替换 formatter 实例。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from providers.base import ProviderResponse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  通用纯文本拒绝提示（三种 formatter 共用）
# ---------------------------------------------------------------------------
_UNIVERSAL_RETRY_HINT = (
    "[SYSTEM] Your last output was rejected because it did not end the task properly. "
    "Every task must conclude by calling the `finish` tool (or `reply` for interim updates). "
    "Free-form text without a tool call is discarded by the runtime. "
    "Please review the tool calling instructions in your system prompt and call `finish` now.\n\n"
    "你的上一条输出被系统丢弃了——没有调用 finish 工具就无法提交结果。请现在调用 finish。"
)


# ---------------------------------------------------------------------------
#  工具调用统一表示
# ---------------------------------------------------------------------------

@dataclass
class ParsedToolCall:
    """从 LLM 响应中解析出的工具调用，与 provider 格式无关。"""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
#  抽象基类
# ---------------------------------------------------------------------------

class ToolFormatter(ABC):
    """工具调用格式的抽象层。

    六个核心职责：
    1. inject_tool_definitions: 将工具定义注入到 LLM 调用参数中
    2. parse_tool_calls: 从 LLM 响应中提取工具调用
    3. format_tool_result: 将工具执行结果格式化为消息
    4. build_assistant_message: LLM 回复后，构建写入对话历史的消息
    5. build_retry_hint: 纯文本响应需要重试时的提示文案
    6. message_to_llm: 将存储格式的消息转换为当前模式的 LLM API 格式（反序列化方向）
    """

    # [2026-05-01] formatter 拆分为 native / fake-native / json 三种模式。
    # 目的：让真原生工具调用不再复用旧的 fake-native 文本历史转换。
    # 子类应覆盖此属性，标识当前 formatter 的模式名（"native" / "fake-native" / "json"）。
    # build_llm_messages 根据此值判断消息是否需要跨模式转换。
    mode: str = ""

    @abstractmethod
    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """将工具定义注入到调用参数中。

        返回 (修改后的 system_prompt, API tools 参数或 None)。
        native/fake-native 模式: prompt 不变, 返回 tools 列表。
        json 模式:              工具定义注入 prompt, 返回 None（不传 tools 参数）。
        """
        ...

    @abstractmethod
    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """从 LLM 响应中提取工具调用。

        native/fake-native 模式: 直接转换 response.tool_calls。
        json 模式:              从 response.text 中解析特定标记块。
        """
        ...

    @abstractmethod
    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """将工具执行结果格式化为消息 dict（L1 存储层）。

        [2026-05-08] 所有模式统一存储为 role=tool + tool_call_id 结构化格式。
        L2 读取时由各 formatter 的 message_to_llm 按需转换为模型可消费格式。
        """
        ...

    def build_assistant_message(
        self, response: ProviderResponse, text: str, tool_calls: list,
    ) -> dict[str, Any]:
        """L1 存储层（通用）：content 只存纯文本，tool_calls 存为同级结构化属性。

        存储逻辑与格式无关，基类统一实现。子类负责 L2 读取（message_to_llm）
        时按各自格式将 tool_calls 还原为模型可消费的文本。

        Args:
            response: LLM 原始响应对象（未使用，保留签名兼容）。
            text: 响应中的纯文本部分。
            tool_calls: 响应中的工具调用列表（ToolCall 对象）。

        Returns:
            消息 dict，包含 role、content，以及可选的 tool_calls 列表。
        """
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": text.strip() if text else "",
        }
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": dict(tc.arguments or {})}
                for tc in tool_calls
            ]
        return msg

    @abstractmethod
    def build_retry_hint(self) -> str:
        """纯文本响应需要重试时的提示文案。"""
        ...

    @abstractmethod
    def message_to_llm(self, message_dict: dict) -> dict:
        """将存储格式的消息 dict 转换为 LLM API 可消费的格式（反序列化方向）。

        剥离 _ 开头的内部字段（_meta, _dynamic, _ephemeral 等），
        原样透传 role 和 content。不做跨模式 role 修补——
        那是 build_llm_messages 路由层的职责。
        """
        ...

    def get_plain_text(self, response: ProviderResponse) -> str | None:
        """从响应中提取纯文本部分，去除工具调用标记。

        默认实现直接返回 response.text。子类可覆盖。
        """
        return response.text


# ---------------------------------------------------------------------------
#  Native 模式：真正原生 tool calling
# ---------------------------------------------------------------------------

class NativeToolFormatter(ToolFormatter):
    """使用真正原生 tool calling 的工具格式。

    [2026-05-01] 新增真 native formatter，原因是旧 NativeToolFormatter
    实际会把 assistant.tool_calls 压成 user 文本，不能满足原生工具调用
    对 assistant.tool_calls 与 role=tool/tool_call_id 配对的要求。
    这里保留 L1 存储中的结构化 tool_calls，并剥离内部字段后直接交给
    provider 层处理，避免在 L2 再做 fake-native 文本转换。
    """

    # 标识当前 formatter 模式，供 build_llm_messages 判断是否需要跨模式转换。
    mode = "native"

    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """Native 模式：prompt 不变，原样返回 tools 列表。"""
        return system_prompt, tools if tools else None

    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """Native 模式：直接转换 ProviderResponse.tool_calls。"""
        if not response.tool_calls:
            return []
        return [
            ParsedToolCall(
                id=tc.id,
                name=tc.name,
                arguments=dict(tc.arguments or {}),
            )
            for tc in response.tool_calls
        ]

    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """Native 模式：生成 role=tool 的真原生工具结果消息。"""
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": result_text,
        }

    def message_to_llm(self, message_dict: dict) -> dict:
        """Native 模式 L2 读取转换。

        [2026-05-01] 真 native 的目标是保留 assistant.tool_calls 与
        role=tool/tool_call_id。剥离内部字段后，将存储格式的 tool_calls
        转换回 chat/completions API 格式：
        存储: {id, name, arguments(dict)}
        API:  {id, type: "function", function: {name, arguments: "<json>"}}
        """
        clean = {k: v for k, v in message_dict.items() if not k.startswith('_')}
        # 将简化存储格式的 tool_calls 转换回 API 原生格式
        if 'tool_calls' in clean and isinstance(clean['tool_calls'], list):
            api_calls = []
            for tc in clean['tool_calls']:
                if isinstance(tc, dict):
                    args = tc.get('arguments', {})
                    args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                    api_calls.append({
                        'id': tc.get('id', ''),
                        'type': 'function',
                        'function': {
                            'name': tc.get('name', ''),
                            'arguments': args_str,
                        },
                    })
            clean['tool_calls'] = api_calls
        return clean

    def build_retry_hint(self) -> str:
        return _UNIVERSAL_RETRY_HINT


# ---------------------------------------------------------------------------
#  Fake Native 模式：旧的文本化原生工具调用兼容层
# ---------------------------------------------------------------------------

class FakeNativeToolFormatter(ToolFormatter):
    """旧 NativeToolFormatter 的兼容实现。

    [2026-05-01] 旧实现命名为 FakeNativeToolFormatter，原因是它并不
    保留原生 role=assistant/tool_calls 与 role=tool，而是把工具调用历史
    写成 [Tool call history record: ...] 文本并改为 role=user。
    保留此类的目的，是让旧配置和旧消息历史可以继续按原行为工作。
    """

    # 标识当前 formatter 模式，供 build_llm_messages 判断是否需要跨模式转换。
    mode = "fake-native"

    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """Fake Native 模式：prompt 不变，原样返回 tools 列表。"""
        return system_prompt, tools if tools else None

    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """Native 模式：直接转换 ProviderResponse.tool_calls。"""
        if not response.tool_calls:
            return []
        return [
            ParsedToolCall(
                id=tc.id,
                name=tc.name,
                arguments=dict(tc.arguments or {}),
            )
            for tc in response.tool_calls
        ]

    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """Fake Native 模式：L1 统一存储为结构化 role=tool 格式。

        [2026-05-08] 与 NativeToolFormatter 保持一致的存储格式。
        旧实现存 role=user 纯文本，导致 L1 存储层分裂：
        assistant 消息有结构化 tool_calls，但 result 是纯文本，
        跨模式读取时无法正确配对。
        现在统一存 role=tool + tool_call_id，message_to_llm 读取时
        再按 fake-native 模式转为 user 文本。
        """
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": result_text,
        }

    def message_to_llm(self, message_dict: dict) -> dict:
        """Fake Native 模式 L2 读取转换。

        从同级 tool_calls 字段读取工具调用，拼接为 [Tool call history record] 文本。
        兼容旧消息：没有 tool_calls 字段时，回退到 _meta.raw_parts。
        """
        # [2026-05-07] Convert native role=tool to fake-native user text.
        # Why: native tool_result (role=tool + tool_call_id) becomes orphaned when
        # its paired assistant.tool_calls is converted to [Tool call history record]
        # text. Anthropic/Gemini require strict 1:1 tool_use/tool_result pairing.
        # Fix: rewrite as role=user 'Tool result for "name":\n...' to match
        # format_tool_result() output.
        if message_dict.get('role') == 'tool':
            tool_name = message_dict.get('name', '') or ''
            content = message_dict.get('content', '') or ''
            return {
                'role': 'user',
                'content': f'Tool result for "{tool_name}":\n{content}',
            }

        clean = {k: v for k, v in message_dict.items()
                 if not k.startswith('_') and k != 'tool_calls'}

        # 优先从同级 tool_calls 字段读取（新格式）
        tool_calls = list(message_dict.get('tool_calls', []))

        # [refactor 2026-04-18] raw_parts → metadata.legacy.raw_parts
        # 回退：旧消息没有 tool_calls 字段，从 _meta 中提取
        if not tool_calls:
            meta = message_dict.get('_meta', {})
            if isinstance(meta, dict):
                # 新格式：metadata.legacy.raw_parts（由 from_dict 迁移而来）
                _legacy = (meta.get('metadata') or {}).get('legacy') or {}
                _raw_parts = _legacy.get('raw_parts', [])
                # 旧格式兼容：直接读 raw_parts
                if not _raw_parts:
                    _raw_parts = meta.get('raw_parts', [])
                for part in _raw_parts:
                    if isinstance(part, dict) and 'tool_calls' in part:
                        tool_calls.extend(part['tool_calls'])

        if tool_calls:
            parts: list[str] = []
            original_content = clean.get('content', '')
            if original_content and original_content != '[tool_call]':
                parts.append(original_content)
            for tc in tool_calls:
                tc_args = json.dumps(tc.get('arguments', {}), ensure_ascii=False)
                parts.append(
                    f"[Tool call history record: {tc.get('name', '')} "
                    f"was executed with args: {tc_args}]"
                )
            clean['content'] = "\n".join(parts) or '[tool_call]'
            clean['role'] = 'user'

        return clean

    def build_retry_hint(self) -> str:
        return _UNIVERSAL_RETRY_HINT


# ---------------------------------------------------------------------------
#  JSON 模式：文本内嵌工具调用（预留接口）
# ---------------------------------------------------------------------------

class JsonToolFormatter(ToolFormatter):
    """使用 JSON 文本块内嵌工具调用的格式。

    适用于不支持原生 function calling 的模型，
    或需要绕过 function calling 限制的场景。

    工具定义注入 system prompt，模型以 <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>>
    格式输出工具调用，引擎从文本中解析出调用请求。
    """

    # 标识当前 formatter 模式，供 build_llm_messages 判断是否需要跨模式转换
    mode = "json"

    CALL_OPEN_TAG = "<<<TOOL_CALL>>>"
    CALL_CLOSE_TAG = "<<<END_TOOL_CALL>>>"

    _TOOL_CALL_PATTERN = re.compile(
        r'<<<TOOL_CALL>>>\s*(\{.*?\})\s*<<<END_TOOL_CALL>>>',
        re.DOTALL,
    )
    _TOOL_CALL_UNCLOSED = re.compile(
        r'<<<TOOL_CALL>>>\s*(\{.*)',
        re.DOTALL,
    )

    # ------------------------------------------------------------------
    #  inject_tool_definitions
    # ------------------------------------------------------------------

    def inject_tool_definitions(
        self,
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """将工具定义转为 markdown 文本，追加到 system_prompt 末尾。

        返回 (修改后的 system_prompt, None)。不传 API tools 参数。
        """
        if not tools:
            return system_prompt, None

        lines: list[str] = [
            "",
            "",
            "[TOOL_USE_INSTRUCTIONS]",
            "",
            "你可以使用以下工具。需要调用工具时，在回复中输出以下格式的 JSON 块：",
            "",
            "<<<TOOL_CALL>>>",
            '{"name": "工具名称", "arguments": {参数对象}}',
            "<<<END_TOOL_CALL>>>",
            "",
            "规则：",
            "1. 每个工具调用必须用 <<<TOOL_CALL>>> 和 <<<END_TOOL_CALL>>> 包裹",
            "2. JSON 必须合法，name 是工具名称，arguments 是参数对象",
            "3. 可以在一条回复中调用多个工具，每个用独立的边界标记包裹",
            "4. 可以在工具调用前后附带文字说明",
            "5. 完成任务时，必须调用 finish 工具提交最终结果",
            "6. 发送中间进度时，使用 reply 工具",
            "",
            "## 可用工具",
            "",
        ]

        for tool in tools:
            lines.append(self._render_tool(tool))

        lines.append("[/TOOL_USE_INSTRUCTIONS]")

        return system_prompt + "\n".join(lines), None

    def _render_tool(self, tool: dict[str, Any]) -> str:
        """将单个工具 spec 渲染为 markdown 文本。

        支持两种输入格式：
        - OpenAI 格式: {type: "function", function: {name, description, parameters}}
        - Registry 格式: {name, description, input_schema}
        """
        if "function" in tool:
            func = tool["function"]
            name = func.get("name", "")
            description = func.get("description", "")
            schema = func.get("parameters", {})
        else:
            name = tool.get("name", "")
            description = tool.get("description", "")
            schema = tool.get("input_schema", {})

        parts: list[str] = [f"### {name}", ""]
        if description:
            parts.append(description)
            parts.append("")

        properties = schema.get("properties", {})
        required_set = set(schema.get("required", []))

        if properties:
            parts.append("参数：")
            for pname, pinfo in properties.items():
                ptype = pinfo.get("type", "any")
                req_label = "必需" if pname in required_set else "可选"
                pdesc = pinfo.get("description", "")
                parts.append(f"- {pname} ({ptype}, {req_label}): {pdesc}")
            parts.append("")

        example_args = self._generate_example_args(schema)
        example_json = json.dumps(
            {"name": name, "arguments": example_args}, ensure_ascii=False,
        )
        parts.append("示例：")
        parts.append("<<<TOOL_CALL>>>")
        parts.append(example_json)
        parts.append("<<<END_TOOL_CALL>>>")
        parts.append("")

        return "\n".join(parts)

    @staticmethod
    def _generate_example_args(schema: dict[str, Any]) -> dict[str, Any]:
        """根据 JSON Schema 自动生成示例参数（仅必需参数）。"""
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        result: dict[str, Any] = {}
        for pname, pinfo in props.items():
            if pname not in required:
                continue
            ptype = pinfo.get("type", "string")
            if "enum" in pinfo and pinfo["enum"]:
                result[pname] = pinfo["enum"][0]
            elif ptype == "string":
                result[pname] = ""
            elif ptype == "integer":
                result[pname] = 0
            elif ptype == "number":
                result[pname] = 0
            elif ptype == "boolean":
                result[pname] = False
            elif ptype == "array":
                result[pname] = []
            elif ptype == "object":
                result[pname] = {}
            else:
                result[pname] = ""
        return result

    # ------------------------------------------------------------------
    #  parse_tool_calls
    # ------------------------------------------------------------------

    def parse_tool_calls(self, response: ProviderResponse) -> list[ParsedToolCall]:
        """从 response.text 中解析 <<<TOOL_CALL>>>...<<<END_TOOL_CALL>>> 块。

        容错策略：
        1. 标准正则匹配所有完整块
        2. JSON 解析失败时尝试 raw_decode 提取第一个对象
        3. 检测到开始标记但无结束标记时，尝试到文本末尾截取

        正文处理策略：
        模型在工具调用之外输出的自由正文（plain text）不会被投递给用户，
        但会通过 build_assistant_message 保留在 assistant 消息的 content 字段中，
        使 LLM 在下一轮能看到自己说过的话（经 message_to_llm 重建为模型可消费格式）。
        用户可见的输出只通过 finish / reply 工具调用产生。
        """
        if not response.text:
            return []

        calls: list[ParsedToolCall] = []

        for match in self._TOOL_CALL_PATTERN.finditer(response.text):
            raw_json = match.group(1)
            parsed = self._try_parse_json(raw_json)
            if parsed is None:
                continue
            name = parsed.get("name")
            if not name:
                continue
            arguments = parsed.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ParsedToolCall(
                id=f"jt_{uuid.uuid4().hex[:8]}",
                name=str(name),
                arguments=arguments,
            ))

        # 回退：检测到开始标记但没有结束标记
        if not calls:
            match = self._TOOL_CALL_UNCLOSED.search(response.text)
            if match:
                raw_json = match.group(1).strip()
                parsed = self._try_parse_json(raw_json)
                if parsed and parsed.get("name"):
                    arguments = parsed.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {}
                    calls.append(ParsedToolCall(
                        id=f"jt_{uuid.uuid4().hex[:8]}",
                        name=str(parsed["name"]),
                        arguments=arguments,
                    ))

        # 自由正文不合成为 reply：正文保留在 build_assistant_message 存储的
        # assistant 消息 content 中，LLM 下轮可见，用户不可见。
        # 原 Fix 2 在此处把正文合成为 reply tool_call 追加到 calls 尾部，
        # 导致模型已显式调用 reply 时产生重复消息，现已删除。

        return calls

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | None:
        """尝试从文本中解析 JSON 对象，带容错。"""
        # 标准解析
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # 回退：用 raw_decode 提取第一个 JSON 对象
        try:
            decoder = json.JSONDecoder()
            idx = text.find("{")
            if idx >= 0:
                obj, _ = decoder.raw_decode(text, idx)
                if isinstance(obj, dict):
                    return obj
        except (json.JSONDecodeError, ValueError):
            pass
        log.warning("JsonToolFormatter: JSON 解析失败: %.200s", text)
        return None

    def get_plain_text(self, response: ProviderResponse) -> str | None:
        """从响应中提取纯文本，去除工具调用块。"""
        if not response.text:
            return None
        plain = self._TOOL_CALL_PATTERN.sub("", response.text).strip()
        # 也清理未闭合的块
        plain = self._TOOL_CALL_UNCLOSED.sub("", plain).strip()
        return plain if plain else None

    # ------------------------------------------------------------------
    #  format_tool_result
    # ------------------------------------------------------------------

    def build_assistant_message(
        self, response: ProviderResponse, text: str, tool_calls: list,
    ) -> dict[str, Any]:
        """JSON 模式：content 只存剔除 <<<TOOL_CALL>>> 标记后的纯文本。

        resp.text 在 JSON 模式下包含完整的 tool call 标记块，
        直接存入 content 会导致 message_to_llm 重建时产生重复。
        此处用 get_plain_text 剥离标记，只保留自由正文部分。
        """
        plain = (self.get_plain_text(response) or "") if response else (text or "")
        return super().build_assistant_message(response, plain, tool_calls)

    def format_tool_result(self, call: ParsedToolCall, result_text: str) -> dict[str, Any]:
        """JSON 模式：L1 统一存储为结构化 role=tool 格式。

        [2026-05-08] 与 Native/FakeNative 保持一致的存储格式。
        message_to_llm 读取时再转为 user 文本。
        """
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": result_text,
        }

    def message_to_llm(self, message_dict: dict) -> dict:
        """JSON 模式 L2 读取转换：从 tool_calls 重建 <<<TOOL_CALL>>> 块拼入 content。

        从同级 tool_calls 字段读取工具调用。
        兼容旧消息：没有 tool_calls 字段时，回退到 _meta.raw_parts。
        """
        # [2026-05-08] Convert native role=tool to user text.
        # JSON 模式不走原生工具协议，tool result 以 user 文本形式呈现。
        if message_dict.get('role') == 'tool':
            tool_name = message_dict.get('name', '') or ''
            content = message_dict.get('content', '') or ''
            return {
                'role': 'user',
                'content': f'Tool result for "{tool_name}":\n{content}',
            }

        clean = {k: v for k, v in message_dict.items()
                 if not k.startswith('_') and k != 'tool_calls'}

        # 优先从同级 tool_calls 字段读取（新格式）
        tool_calls = list(message_dict.get('tool_calls', []))

        # [refactor 2026-04-18] raw_parts → metadata.legacy.raw_parts
        # 回退：旧消息没有 tool_calls 字段，从 _meta 中提取
        if not tool_calls:
            meta = message_dict.get('_meta', {})
            if isinstance(meta, dict):
                # 新格式：metadata.legacy.raw_parts（由 from_dict 迁移而来）
                _legacy = (meta.get('metadata') or {}).get('legacy') or {}
                _raw_parts = _legacy.get('raw_parts', [])
                # 旧格式兼容：直接读 raw_parts
                if not _raw_parts:
                    _raw_parts = meta.get('raw_parts', [])
                for part in _raw_parts:
                    if isinstance(part, dict) and 'tool_calls' in part:
                        tool_calls.extend(part['tool_calls'])

        if tool_calls:
            parts = []
            content = clean.get('content', '')
            if content:
                # 防御性清理：剥离可能残留的 tool call 标记，避免与下方重建的重复
                content = self._TOOL_CALL_PATTERN.sub("", content).strip()
                content = self._TOOL_CALL_UNCLOSED.sub("", content).strip()
                if content:
                    parts.append(content)
            for tc in tool_calls:
                tc_json = json.dumps(
                    {"name": tc.get("name", ""), "arguments": tc.get("arguments", {})},
                    ensure_ascii=False,
                )
                parts.append(f"<<<TOOL_CALL>>>\n{tc_json}\n<<<END_TOOL_CALL>>>")
            clean['content'] = "\n".join(parts)

        return clean

    def build_retry_hint(self) -> str:
        return _UNIVERSAL_RETRY_HINT


# ---------------------------------------------------------------------------
#  工厂函数
# ---------------------------------------------------------------------------

def _normalize_tool_mode(mode: str | None) -> str:
    """归一化工具模式名称。

    [2026-05-01] 新增 fake-native 后需要同时兼容历史写法 fake_native。
    空值和未知值统一落到 fake-native，目的不是静默启用新 native，
    而是保护未显式配置 tool_mode 的旧节点与旧历史。
    """
    raw = str(mode or "fake-native").strip().lower().replace("_", "-")
    if raw in {"native", "fake-native", "json"}:
        return raw
    return "fake-native"


def create_tool_formatter(mode: str = "fake-native") -> ToolFormatter:
    """根据 mode 创建对应的 ToolFormatter 实例。

    Args:
        mode: "native"、"fake-native" 或 "json"。默认 "fake-native"。

    Returns:
        ToolFormatter 实例。
    """
    normalized = _normalize_tool_mode(mode)
    if normalized == "json":
        return JsonToolFormatter()
    if normalized == "native":
        return NativeToolFormatter()
    return FakeNativeToolFormatter()


# ---------------------------------------------------------------------------
#  工具结果配对修复
# ---------------------------------------------------------------------------

_FINISH_TOOL_NAME = "finish"
_FINISH_TOOL_RESULT_TEXT = "ok"


def _tool_call_name(call: Any) -> str:
    """Read a tool call name from either Clonoth storage or OpenAI API shape."""
    if not isinstance(call, dict):
        return ""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("name") or function.get("name") or "").strip()


def _tool_call_id(call: Any) -> str:
    """Read a tool call id defensively from a stored tool call dict."""
    return str(call.get("id") or "").strip() if isinstance(call, dict) else ""


def _tool_call_arguments(call: Any) -> dict[str, Any]:
    """Read tool call arguments from simplified or OpenAI API format."""
    if not isinstance(call, dict):
        return {}
    raw = call.get("arguments")
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    if raw is None:
        raw = function.get("arguments")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _is_finish_tool_name(name: str) -> bool:
    """Return whether a tool name is the real finish API tool."""
    return str(name or "").strip() == _FINISH_TOOL_NAME


def _message_meta(message: dict[str, Any]) -> dict[str, Any]:
    """Return message metadata from either runtime or persisted JSONL shape."""
    # Why: runtime history uses _meta, while ConversationStore JSONL uses meta.
    # How: read both keys without mutating the message. Purpose: let the same
    # repair routine protect build_llm_messages, summaries, and migration tests.
    meta = message.get("_meta") if isinstance(message.get("_meta"), dict) else message.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _message_tool_mode(message: dict[str, Any]) -> str:
    """Read and normalize the tool mode stored on a message."""
    return _normalize_tool_mode(str(_message_meta(message).get("tool_mode") or "fake-native"))


def _parse_text_tool_result(content: Any) -> tuple[str, str] | None:
    """Parse fake-native text tool results into (name, output)."""
    if not isinstance(content, str):
        return None
    prefix = 'Tool result for "'
    if not content.startswith(prefix):
        return None
    end = content.find('":')
    if end <= len(prefix):
        return None
    name = content[len(prefix):end]
    output = content[end + 2:]
    if output.startswith("\n"):
        output = output[1:]
    elif output.startswith(" "):
        # [2026-05-07] 兼容旧单行 fake-native 结果。
        # 原因：旧文本可能写成 `Tool result for "finish": completed`，冒号后有一个分隔空格。
        # 做法：只去掉这个协议分隔空格，不对多行真实结果做 strip。
        # 目的：修复旧 finish result 时保留原结果文本的语义。
        output = output[1:]
    return name, output


def _is_tool_result_message(message: dict[str, Any]) -> bool:
    """Return whether a stored message represents a tool result."""
    if message.get("role") == "tool":
        return True
    if str(message.get("message_type") or "") == "tool_result":
        return True
    meta = _message_meta(message)
    if str(meta.get("message_type") or "") == "tool_result":
        return True
    return _parse_text_tool_result(message.get("content")) is not None


def _tool_result_name(message: dict[str, Any]) -> str:
    """Return the tool name carried by a tool-result message, when known."""
    explicit = str(message.get("name") or "").strip()
    if explicit:
        return explicit
    meta_name = str(_message_meta(message).get("control_tool_name") or "").strip()
    if meta_name:
        # Why: old broken rows used control_tool_name to mark finish results.
        # How: read it only as a legacy name hint. Purpose: migrate those rows
        # without preserving the old non-persistent control semantics.
        return meta_name
    parsed = _parse_text_tool_result(message.get("content"))
    return parsed[0] if parsed else ""


def _tool_result_call_id(message: dict[str, Any]) -> str:
    """Return the assistant tool_call id referenced by a tool result."""
    return str(message.get("tool_call_id") or "").strip()


def _assistant_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized tool calls from one assistant message."""
    if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
        return []
    calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = _tool_call_name(call)
        if not name:
            continue
        calls.append({
            "id": _tool_call_id(call),
            "name": name,
            "arguments": _tool_call_arguments(call),
        })
    return calls


def _make_finish_tool_result(call: dict[str, Any], tool_mode: str) -> dict[str, Any]:
    """Build a missing ordinary finish tool_result for old dangling histories."""
    mode = _normalize_tool_mode(tool_mode)
    call_id = str(call.get("id") or "")
    if mode == "native":
        # Why: true-native providers require role=tool with the original call id.
        # How: synthesize the same ok result that the engine now writes at finish.
        # Purpose: old assistant.finish calls can be repaired instead of deleted.
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": _FINISH_TOOL_NAME,
            "content": _FINISH_TOOL_RESULT_TEXT,
            "_meta": {"tool_mode": "native", "message_type": "tool_result"},
        }
    meta: dict[str, Any] = {"message_type": "tool_result"}
    if mode != "fake-native":
        meta["tool_mode"] = mode
    return {
        "role": "user",
        "content": f'Tool result for "{_FINISH_TOOL_NAME}":\n{_FINISH_TOOL_RESULT_TEXT}',
        "_meta": meta,
    }


def repair_tool_result_pairing_with_stats(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Remove orphan tool results while preserving valid tool-call pairs.

    [2026-05-07] finish is no longer treated as an ephemeral control result.
    Why: provider-native histories require every persisted assistant.tool_call to
    keep its matching tool_result, including finish. How: track the pending tool
    calls from the most recent assistant message, keep only matching result rows,
    convert old paired fake-native finish result text into a native role=tool row,
    and remove unpaired native result rows. Purpose: fix old orphan pollution
    without deleting valid finish or business-tool history.
    """
    stats = {
        "removed_orphan_finish_results": 0,
        "removed_orphan_tool_results": 0,
        "inserted_missing_finish_results": 0,
        "kept_paired_finish_results": 0,
        "fixed_orphan_tool_calls": 0,
    }
    result: list[dict[str, Any]] = []
    pending_by_id: dict[str, dict[str, Any]] = {}
    pending_order: list[dict[str, Any]] = []
    pending_tool_mode = "fake-native"
    pending_assistant_result_idx: int = -1  # index in result[] of the assistant that created pending

    def _strip_unmatched_tool_calls() -> None:
        """Strip unmatched tool_calls from the pending assistant in result.

        [2026-05-09] When pending_order is non-empty (some tool_calls never got
        a result), go back to the assistant message in result[] and convert the
        unmatched tool_calls to text summaries. Keep matched ones intact.
        """
        nonlocal pending_assistant_result_idx
        if not pending_order or pending_assistant_result_idx < 0:
            return
        if pending_assistant_result_idx >= len(result):
            return
        ast_msg = result[pending_assistant_result_idx]
        if not ast_msg.get('tool_calls'):
            return
        unmatched_ids = {str(c.get('id', '')) for c in pending_order if c.get('id')}
        if not unmatched_ids:
            return
        kept = []
        text_parts: list[str] = []
        for tc in ast_msg['tool_calls']:
            tc_id = str(tc.get('id', ''))
            if tc_id in unmatched_ids:
                fn = tc.get('function', {}) if isinstance(tc.get('function'), dict) else {}
                name = fn.get('name', '') or tc.get('name', '')
                text_parts.append(f'[Tool call: {name}]')
                stats['fixed_orphan_tool_calls'] += 1
            else:
                kept.append(tc)
        if kept:
            ast_msg['tool_calls'] = kept
        else:
            ast_msg.pop('tool_calls', None)
        if text_parts:
            existing = ast_msg.get('content', '') or ''
            if isinstance(existing, list):
                existing = ' '.join(
                    c.get('text', '') for c in existing
                    if isinstance(c, dict) and c.get('type') == 'text'
                )
            summary = ' '.join(text_parts)
            ast_msg['content'] = (existing.strip() + '\n' + summary).strip() if existing.strip() else summary
        pending_assistant_result_idx = -1

    def _reset_pending() -> None:
        nonlocal pending_assistant_result_idx
        _strip_unmatched_tool_calls()
        pending_by_id.clear()
        pending_order.clear()
        pending_assistant_result_idx = -1

    def _set_pending_from_assistant(message: dict[str, Any]) -> None:
        nonlocal pending_tool_mode, pending_assistant_result_idx
        _reset_pending()
        calls = _assistant_tool_calls(message)
        if not calls:
            return
        pending_tool_mode = _message_tool_mode(message)
        pending_order.extend(calls)
        for call in calls:
            cid = str(call.get("id") or "")
            if cid:
                pending_by_id[cid] = call
        pending_assistant_result_idx = len(result) - 1  # the assistant just appended

    def _consume_matching_result(message: dict[str, Any]) -> dict[str, Any] | None:
        call_id = _tool_result_call_id(message)
        name = _tool_result_name(message)
        if call_id and call_id in pending_by_id:
            call = pending_by_id.pop(call_id)
            pending_order[:] = [item for item in pending_order if str(item.get("id") or "") != call_id]
            return call
        for idx, call in enumerate(list(pending_order)):
            call_name = str(call.get("name") or "")
            call_has_id = bool(str(call.get("id") or ""))
            if message.get("role") == "tool":
                # [2026-05-07] role=tool 结果不能只按 name 配对到有 id 的调用。
                # 原因：原生 provider 校验 tool_call_id；缺 id 的结果即使同名也会成为坏历史。
                # 做法：只有 assistant 调用本身也没有 id 时才允许 name fallback。
                # 目的：避免普通业务工具结果被误判为已配对。
                if call_has_id:
                    continue
            if name and call_name == name:
                pending_order.pop(idx)
                cid = str(call.get("id") or "")
                if cid:
                    pending_by_id.pop(cid, None)
                return call
        return None

    def _repair_paired_result_message(message: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
        call_id = str(call.get("id") or "")
        call_name = str(call.get("name") or "")
        if message.get("role") == "tool":
            repaired = dict(message)
            # [2026-05-07] 已配对的原生结果补齐缺失字段。
            # 原因：旧 JSONL 可能保存了 name 或 tool_call_id 不完整的 role=tool 行。
            # 做法：从刚匹配到的 assistant.tool_call 回填缺失值。
            # 目的：后续 provider 转换拿到完整的工具结果结构。
            if call_id:
                repaired.setdefault("tool_call_id", call_id)
            if call_name:
                repaired.setdefault("name", call_name)
            return repaired

        parsed = _parse_text_tool_result(message.get("content"))
        if parsed is None:
            return dict(message)
        result_name, output = parsed
        if _normalize_tool_mode(pending_tool_mode) != "native":
            return dict(message)

        # [2026-05-07] 修复旧 fake-native 文本结果与 native assistant.finish 的配对。
        # 原因：早期存储会把 `Tool result for "finish": completed` 写成 user 文本，
        # 但它紧跟 native assistant.finish 时本质上是该 tool_call 的结果。
        # 做法：转换为 role=tool，并沿用原输出文本而不是强行改写为 ok。
        # 目的：兼容旧历史，同时保持 provider 原生工具协议一致。
        meta = _message_meta(message)
        meta["tool_mode"] = "native"
        meta["message_type"] = "tool_result"
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": result_name or call_name,
            "content": output,
            "_meta": meta,
        }

    for original in messages:
        if not isinstance(original, dict):
            continue
        msg = dict(original)
        if _is_tool_result_message(msg):
            matched_call = _consume_matching_result(msg)
            result_name = _tool_result_name(msg) or str((matched_call or {}).get("name") or "")
            if matched_call is not None:
                if _is_finish_tool_name(result_name):
                    stats["kept_paired_finish_results"] += 1
                result.append(_repair_paired_result_message(msg, matched_call))
                continue

            parsed = _parse_text_tool_result(msg.get("content"))
            if msg.get("role") == "tool":
                # [2026-05-07] 通用清洗所有原生孤儿 tool_result。
                # 原因：Anthropic/Gemini 不只校验 finish，普通业务工具孤儿结果也会报错。
                # 做法：role=tool 未匹配到当前 assistant.tool_call 时删除。
                # 目的：保留完整配对，移除无法配对的半截结果。
                if _is_finish_tool_name(result_name):
                    stats["removed_orphan_finish_results"] += 1
                else:
                    stats["removed_orphan_tool_results"] += 1
                continue
            if parsed is not None and _is_finish_tool_name(parsed[0]):
                # [2026-05-07] 只删除孤儿 finish 文本结果。
                # 原因：旧 finish completed 文本没有配对时会污染摘要和 provider 历史。
                # 做法：非 finish 的 fake-native 业务工具文本仍保留给摘要阅读。
                # 目的：既清理控制工具旧污染，又不误删业务工具结果。
                stats["removed_orphan_finish_results"] += 1
                continue
            result.append(msg)
            continue

        if pending_order:
            # [2026-05-09] 遇到非工具结果消息时关闭当前 pending 窗口。
            # _reset_pending() 内部调用 _strip_unmatched_tool_calls()，
            # 自动将未配对的 tool_calls 转为文本摘要。
            _reset_pending()
        result.append(msg)
        if msg.get("role") == "assistant":
            _set_pending_from_assistant(msg)

    if pending_order:
        # [2026-05-09] _reset_pending() calls _strip_unmatched_tool_calls()
        # which handles trailing orphans automatically.
        _reset_pending()

    return result, stats


def repair_tool_result_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return history with old orphan finish results repaired or removed."""
    repaired, _stats = repair_tool_result_pairing_with_stats(messages)
    return repaired


def sanitize_assistant_control_tools(
    message: dict[str, Any],
    *,
    deliver_control_text: bool = True,
    suppressed_control_call_ids: set[str] | None = None,
    suppress_all_control_text: bool = False,
    drop_if_only_control: bool = False,
) -> dict[str, Any] | None:
    """Legacy compatibility wrapper that no longer strips finish tool calls.

    Why: external callers may still import this helper, but finish is now a real
    persistent API tool. How: return the assistant message unchanged and ignore the
    old control-flow arguments. Purpose: avoid reintroducing finish filtering while
    keeping the public helper import-safe during the transition.
    """
    return dict(message)


def sanitize_control_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy name for repair_tool_result_pairing.

    Why: existing runner, compactor, and provider-bypass call sites still use the
    old sanitizer name. How: delegate to the new pairing repair routine, which
    preserves valid finish tool_use/tool_result pairs. Purpose: make the semantic
    correction without a noisy cross-file rename in this urgent patch.
    """
    return repair_tool_result_pairing(messages)


# ---------------------------------------------------------------------------
#  反序列化辅助：将存储消息列表转换为 LLM 可消费格式
# ---------------------------------------------------------------------------

def build_llm_messages(
    messages: list[dict],
    current_formatter: ToolFormatter,
) -> list[dict]:
    """将消息列表转换为 LLM 可消费的 dict 列表（反序列化方向）。

    三层管道路由：每条消息根据自身 _meta.tool_mode 选择对应的 formatter，
    每个 formatter 只处理自己模式的消息，内部零 if 补丁。

    路由规则：
    - 没有 _meta 或 tool_mode 的老消息 → FakeNativeToolFormatter（旧消息都是 fake-native 文本兼容格式）
    - tool_mode='fake-native' → FakeNativeToolFormatter
    - tool_mode='native' → NativeToolFormatter（真原生结构化工具调用）
    - tool_mode='json' → JsonToolFormatter

    跳过 _ephemeral 消息（retry hint 等），保留 _dynamic 消息（动态上下文）。
    """
    # [2026-05-07] 先执行工具结果配对清洗。
    # 原因：build_llm_messages 是所有非旁路 provider 的 L2 入口，旧存储中可能已有孤儿 tool_result。
    # 做法：在模式路由前只移除无法配对的结果，完整工具轮包括 finish 都保留。
    # 目的：普通工具和 finish 都按各自 formatter 回放，避免 provider 配对错误。
    cleaned_messages = sanitize_control_tool_history(messages)

    result: list[dict] = []
    for msg in cleaned_messages:
        # 跳过 ephemeral 消息（如 retry hint），它们不应进入 LLM 历史。
        # _dynamic 消息（动态上下文）保留——它们需要被 LLM 看到，
        # message_to_llm 会剥离 _dynamic 标记键但保留消息本身。
        if msg.get('_ephemeral'):
            continue

        # 根据消息自身记录的 tool_mode 选择 formatter
        msg_mode = ''
        meta = msg.get('_meta', {})
        if isinstance(meta, dict):
            msg_mode = meta.get('tool_mode', '')

        # [2026-05-01] 老消息（无 _meta / 无 tool_mode）默认视为 fake-native。
        # 原因：历史上的 native 名称实际就是 fake-native；如果把无标记旧消息
        # 当作新的真 native，会错误保留或发送旧文本化历史之外的结构。
        if not msg_mode:
            msg_mode = 'fake-native'
        else:
            msg_mode = _normalize_tool_mode(msg_mode)

        # [2026-05-14] 统一用 current_formatter 处理所有消息，不按消息自身 mode 路由。
        # 原因：选了 json 模式就应该把所有历史消息（包括 native 存储的）都翻译为
        # json 文本格式，而不是原样保留 role=tool + tool_calls 结构。
        # 各 formatter 的 message_to_llm 已有跨模式兼容逻辑（如 json/fake-native
        # 都能将 role=tool 转为 user 文本）。
        result.append(current_formatter.message_to_llm(msg))
    return result
