from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import load_yaml_dict, load_runtime_config, resolve_env_ref
from providers import registry as provider_registry


@dataclass
class ToolAccess:
    mode: str = "none"  # "none" | "all" | "allowlist"
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass
class SkillAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class MemoryAccess:
    mode: str = "all"  # "all" | "allowlist" | "none"
    allow: list[str] = field(default_factory=list)


@dataclass
class Node:
    id: str
    type: str  # "ai" | "tool" | "script"
    name: str = ""
    description: str = ""
    model: str = ""          # 模型名称，空 = 使用全局默认
    api_key: str = ""        # 可选独立 API key（支持 $ENV{} 语法）
    base_url: str = ""       # 可选独立 base URL（支持 $ENV{} 语法）
    prompt: str | list = ""  # system prompt：字符串模式或 block 列表模式
    # [AutoC 2026-06-09] Why: script nodes need deterministic inline Python without invoking an LLM.
    # How: store the YAML script body verbatim and keep execution timeout on the node model.
    # Purpose: runner can execute type=script nodes directly while preserving existing AI node fields.
    script: str = ""
    script_timeout_sec: float = 30.0
    tool_access: ToolAccess = field(default_factory=ToolAccess)
    skill_access: SkillAccess = field(default_factory=SkillAccess)
    memory_access: MemoryAccess = field(default_factory=MemoryAccess)
    # [2026-05-01] 默认工具模式改为 fake-native。
    # 原因：旧 native 实际是文本化 fake-native；未显式配置的节点应继续旧行为。
    tool_mode: str = "fake-native"  # "fake-native" | "native" | "json"
    # hybrid output_mode: 纯文本输出不再 reject 重试，直接作为隐式 finish 投递给用户。
    # tool_only = 现有行为（强制 finish）；hybrid = 允许纯文本直接投递。
    output_mode: str = "tool_only"  # "tool_only" | "hybrid"
    # [AutoC 2026-06-08] Why: output chain 是 Supervisor 侧 finish 后路由声明，不进入节点 prompt。
    # How: 节点 YAML 可配置 output 为目标 node_id 或 __source__，load_node 只解析并保留原始声明。
    # Purpose: 让节点保持纯处理单元，后续路由统一由 Supervisor 决定。
    output: str = ""
    # [provider-registry 2026-05-03] provider 是 registry key，空字符串表示使用默认 provider。
    # 原因：provider 列表已插件化；做法：不在类型注释中列死内置名称；目的：新增 provider 无需改 Node 定义。
    provider: str = ""
    provider_options: dict = field(default_factory=dict)  # 透传给 provider 的参数（thinking/reasoning 等）
    delegate_targets: list[str] = field(default_factory=list)
    # [2026-05-27] persistent 标记：声明该节点为持久节点。
    # 为什么：子节点在 accumulate 模式下复用 child session，但缺少显式配置标记
    # 来告知引擎该 session 需要接入自动压缩。
    # 怎么改：新增 bool 字段，默认 False。
    # 目的：persistent=true 的节点的 child session 能触发自动上下文压缩，
    # 同时 dispatch 该节点时默认使用 accumulate context_mode。
    persistent: bool = False
    # [2026-05-27] extra: 收集节点 yaml 中 core 不认识的字段，供插件层零 IO 读取。
    # 为什么：memory_book 等插件业务配置不应定义在 Node dataclass 上，
    # 但插件每次从 yaml 文件读取也浪费 IO。
    # 怎么改：load_node 把 yaml 中不属于 core 已知字段的 key-value 全部收进 extra。
    # 目的：插件通过 ToolContext._node_extra 零 IO 拿到自己的配置。
    extra: dict = field(default_factory=dict)


def load_node(workspace_root: Path, node_id: str) -> Node | None:
    nid = (node_id or "").strip()
    if not nid:
        return None
    # 系统节点目录分离：优先从 engine/system_nodes/ 加载内建系统节点，
    # 找不到时回退到 config/nodes/ 用户配置目录。确保系统节点跟随代码仓库同步。
    _sys_path = workspace_root / "engine" / "system_nodes" / f"{nid}.yaml"
    _usr_path = workspace_root / "config" / "nodes" / f"{nid}.yaml"
    _node_path = _sys_path if _sys_path.is_file() else _usr_path
    data = load_yaml_dict(_node_path)
    if not isinstance(data, dict):
        return None
    # kind 默认 "node"，允许省略
    if str(data.get("kind") or "node").strip() != "node":
        return None
    node_type = str(data.get("type") or "ai").strip().lower()
    if node_type not in {"ai", "tool", "script"}:
        return None

    # prompt：字符串→字符串模式，列表→block 列表模式
    raw_prompt = data.get("prompt")
    if isinstance(raw_prompt, list):
        prompt = raw_prompt
    elif isinstance(raw_prompt, str):
        prompt = raw_prompt.strip()
    else:
        prompt = ""

    # [AutoC 2026-06-09] Why: YAML may carry arbitrary values, but script execution expects source text.
    # How: accept only string script bodies and parse a positive numeric timeout with a safe default.
    # Purpose: invalid script metadata cannot crash node loading or leak into extra configuration.
    raw_script = data.get("script")
    script = raw_script if isinstance(raw_script, str) else ""
    try:
        script_timeout_sec = float(data.get("script_timeout_sec", 30.0))
    except Exception:
        script_timeout_sec = 30.0
    if script_timeout_sec <= 0:
        script_timeout_sec = 30.0

    # model：直接值或 $ENV{} 引用
    raw_model = str(data.get("model") or "").strip()
    model = resolve_env_ref(raw_model) if raw_model else ""

    # 可选独立 api_key / base_url
    raw_api_key = str(data.get("api_key") or "").strip()
    api_key = resolve_env_ref(raw_api_key) if raw_api_key else ""
    raw_base_url = str(data.get("base_url") or "").strip()
    base_url = resolve_env_ref(raw_base_url) if raw_base_url else ""

    ta_raw = data.get("tool_access")
    if isinstance(ta_raw, str):
        m = ta_raw.strip().lower()
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none")
    elif isinstance(ta_raw, dict):
        m = str(ta_raw.get("mode") or "none").strip().lower()
        allow = [
            str(x).strip()
            for x in (ta_raw.get("allow") or [])
            if isinstance(x, str) and x.strip()
        ]
        deny = [
            str(x).strip()
            for x in (ta_raw.get("deny") or [])
            if isinstance(x, str) and x.strip()
        ]
        ta = ToolAccess(mode=m if m in {"none", "all", "allowlist"} else "none", allow=allow, deny=deny)
    else:
        ta = ToolAccess()

    sa_raw = data.get("skills")
    if isinstance(sa_raw, str):
        sm = sa_raw.strip().lower()
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all")
    elif isinstance(sa_raw, dict):
        sm = str(sa_raw.get("mode") or "all").strip().lower()
        sa_allow = [str(x).strip() for x in (sa_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        sa = SkillAccess(mode=sm if sm in {"all", "allowlist", "none"} else "all", allow=sa_allow)
    else:
        sa = SkillAccess()

    ma_raw = data.get("memories")
    if isinstance(ma_raw, str):
        mm = ma_raw.strip().lower()
        ma = MemoryAccess(mode=mm if mm in {"all", "allowlist", "none"} else "all")
    elif isinstance(ma_raw, dict):
        mm = str(ma_raw.get("mode") or "all").strip().lower()
        ma_allow = [str(x).strip() for x in (ma_raw.get("allow") or []) if isinstance(x, str) and x.strip()]
        ma = MemoryAccess(mode=mm if mm in {"all", "allowlist", "none"} else "all", allow=ma_allow)
    else:
        ma = MemoryAccess()

    # tool_mode — 节点 yaml 优先，否则用 runtime.yaml engine.tool_mode 全局默认。
    # [2026-05-01] 支持 fake-native / native / json 三种值，并兼容旧写法 fake_native。
    # 空值或非法值回退到 fake-native，目的：不把旧节点静默切到新的真 native。
    _node_tm = data.get("tool_mode")
    if _node_tm is not None:
        raw_tool_mode = str(_node_tm).strip().lower().replace("_", "-")
    else:
        _rt = load_runtime_config(workspace_root)
        raw_tool_mode = str((_rt.get("engine") or {}).get("tool_mode") or "fake-native").strip().lower().replace("_", "-")
    tool_mode = raw_tool_mode if raw_tool_mode in {"fake-native", "native", "json"} else "fake-native"

    # output_mode — 节点 yaml 优先，否则用 runtime.yaml engine.output_mode 全局默认
    # hybrid 模式下纯文本输出直接投递给用户，不 reject 不重试（RFC: rfc_hybrid_output_mode.md）
    _node_om = data.get("output_mode")
    if _node_om is not None:
        raw_output_mode = str(_node_om).strip().lower()
    else:
        _rt_om = load_runtime_config(workspace_root)
        raw_output_mode = str((_rt_om.get("engine") or {}).get("output_mode") or "hybrid").strip().lower()
    output_mode = raw_output_mode if raw_output_mode in {"tool_only", "hybrid"} else "tool_only"

    # [AutoC 2026-06-08] Why: Supervisor 需要从节点声明读取 finish 后的 output chain 目标。
    # How: 解析 YAML 中的 output 字段并保留去空白后的字符串，允许普通 node_id 或 __source__。
    # Purpose: 无 output 字段时保持空字符串，从而完全保留旧路由行为。
    raw_output = str(data.get("output") or "").strip()
    output = raw_output

    # provider — 节点 yaml 可指定 provider 类型
    raw_provider = str(data.get("provider") or "").strip().lower()
    # [provider-registry 2026-05-03] 从 registry 动态校验 provider 名称。
    # 原因：固定白名单会阻止新 provider 插件；做法：读取 ProviderRegistry.list()；
    # 目的：注册成功的 provider 可以直接被节点配置使用，未知值仍回退为空字符串。
    provider = raw_provider if raw_provider in {"", *provider_registry.list()} else ""

    # provider_options — 透传给 provider 的参数（如 Anthropic thinking、OpenAI reasoning 等）
    raw_po = data.get("provider_options")
    provider_options = dict(raw_po) if isinstance(raw_po, dict) else {}

    # delegate_targets
    dt_raw = data.get("delegate_targets")
    delegate_targets: list[str] = [
        str(x).strip() for x in (dt_raw or []) if isinstance(x, str) and x.strip()
    ] if isinstance(dt_raw, list) else []

    # [2026-05-27] persistent 字段解析：从 node yaml 读取 persistent 布尔值。
    # 为什么：支持节点级别的持久化声明，用于控制 context_mode 默认值和自动压缩。
    # 怎么改：解析 yaml 中的 persistent 字段，非布尔值均视为 False。
    persistent = bool(data.get("persistent", False))

    # [2026-05-27] extra dict：收集 yaml 中 core 不认识的字段（如 memory_book），
    # 供插件层通过 ToolContext._node_extra 零 IO 读取。
    _KNOWN_KEYS = {
        "kind", "id", "type", "name", "description", "model", "api_key",
        "base_url", "prompt", "tool_access", "skills", "memories",
        "tool_mode", "output_mode", "output", "provider", "provider_options",
        "delegate_targets", "persistent", "script", "script_timeout_sec",
    }
    extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}

    return Node(
        id=str(data.get("id") or nid).strip(),
        type=node_type,
        name=str(data.get("name") or nid).strip(),
        description=str(data.get("description") or "").strip(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        prompt=prompt,
        script=script,
        script_timeout_sec=script_timeout_sec,
        tool_access=ta,
        skill_access=sa,
        memory_access=ma,
        tool_mode=tool_mode,
        output_mode=output_mode,
        output=output,
        provider=provider,
        provider_options=provider_options,
        delegate_targets=delegate_targets,
        persistent=persistent,
        extra=extra,
    )
