from __future__ import annotations

"""Built-in plugin: create_agent tool for persistent sub-node provisioning.

[2026-05-28] 为什么新建此模块：手动复制 yaml + 修改字段 + 更新 delegate_targets 繁琐且易出错。
怎么做：注册为 engine.builtin 内置工具，通过 PLUGIN_META.tools 声明，与 knowledge_inject.py
中的 save_memory / create_or_update_skill 采用相同的注册路径。
目的：一条工具调用完成子节点创建、记忆目录初始化、委派关系注册。
"""

import re
from pathlib import Path
from typing import Any

import yaml

from toolbox.context import ToolContext

# ---------------------------------------------------------------------------
#  PLUGIN_META — loader auto-discovery 需要的元数据声明
# ---------------------------------------------------------------------------
# hook_points 为空列表：本模块只提供工具，不注册任何 hook handler。
# loader 会正常实例化 handler_class 并注册 tools，只是不绑定任何 hook。
PLUGIN_META = {
    "handler_class": "AgentManageHandler",
    "hook_points": [],
    "priority": 10,
    "tools": [],  # 在文件末尾由工具函数定义后填充
}


class AgentManageHandler:
    """Minimal handler class required by loader auto-discovery.

    [2026-05-28] 为什么需要这个类：loader.py 的 _instantiate_handler 要求
    PLUGIN_META.handler_class 指向一个可实例化的类。本模块没有 hook 行为，
    只需要一个空壳让 loader 走完流程，tools 声明才能被注册到 ToolRegistry。
    """
    name = "agent_manage"
    priority = 10


# ---------------------------------------------------------------------------
#  Validation
# ---------------------------------------------------------------------------
# [2026-05-28] 节点 ID 校验：与 toolbox.builtins.SKILL_NAME_RE 对齐，
# 但节点 ID 允许点号（如 bootstrap.coder），所以单独定义。
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: create_agent returns several structured paths and
    # flags but should expose data.result as its readable transcript. How: put all
    # previous fields under data with the summary. Purpose: align plugin tools with
    # the unified ok/data/error response schema.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: create_agent validation failures should be readable
    # through data.result. How: wrap the error once. Purpose: avoid legacy plugin
    # error payloads during migration.
    text = str(message)
    return {"ok": False, "error": text, "data": {"result": f"ERROR: {text}"}}


# ---------------------------------------------------------------------------
#  create_agent tool
# ---------------------------------------------------------------------------

async def create_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a new persistent sub-node from an existing node yaml template.

    [2026-05-28] 工作流程：
    1. 读取模板节点 yaml（先 config/nodes/，再 engine/system_nodes/）
    2. 替换 id / name / description / persistent / memory_book / provider 字段
    3. 写入 config/nodes/{name}.yaml
    4. 可选：追加到 caller_node_id 的 delegate_targets
    5. 创建 data/memory/{memory_book}/ 目录
    """
    name = str(args.get("name", "")).strip()
    template = str(args.get("template", "")).strip()

    if not name:
        return _err("name is required")
    if not template:
        return _err("template is required")
    if not _NODE_ID_RE.fullmatch(name):
        return _err(f"invalid node id: {name!r} — only [A-Za-z0-9][A-Za-z0-9_.\\-]{{0,63}} allowed")

    display_name = str(args.get("display_name", "")).strip() or name
    description = str(args.get("description", "")).strip()
    # memory_book 默认等于 name，用于记忆 namespace 子目录隔离
    memory_book = str(args.get("memory_book", "")).strip() or name
    persistent = bool(args.get("persistent", True))
    provider = str(args.get("provider", "")).strip()
    caller_node_id = str(args.get("caller_node_id", "")).strip()

    workspace_root = ctx.workspace_root

    # Step 1: 检查目标节点配置是否已存在，避免误覆盖
    target_path = workspace_root / "config" / "nodes" / f"{name}.yaml"
    if target_path.exists():
        return _err(f"node config already exists: config/nodes/{name}.yaml")

    # Step 2: 查找模板 yaml（先 config/nodes/，再 engine/system_nodes/）
    # 与 engine/node.py load_node 的搜索顺序一致
    tmpl_path = workspace_root / "config" / "nodes" / f"{template}.yaml"
    if not tmpl_path.exists():
        tmpl_path = workspace_root / "engine" / "system_nodes" / f"{template}.yaml"
    if not tmpl_path.exists():
        return _err(f"template not found: {template} (checked config/nodes/ and engine/system_nodes/)")

    # Step 3: 解析模板 yaml
    try:
        tmpl_text = tmpl_path.read_text(encoding="utf-8")
        tmpl_data = yaml.safe_load(tmpl_text)
    except Exception as e:
        return _err(f"failed to parse template: {e}")
    if not isinstance(tmpl_data, dict):
        return _err("template is not a valid YAML dict")

    # Step 4: 替换核心字段
    tmpl_data["id"] = name
    tmpl_data["name"] = display_name
    if description:
        tmpl_data["description"] = description
    tmpl_data["persistent"] = persistent
    # memory_book 作为 extra 字段，会被 Node.load_node 收进 extra dict，
    # 供 save_memory 等插件通过 ToolContext._node_extra 零 IO 读取
    tmpl_data["memory_book"] = memory_book
    if provider:
        tmpl_data["provider"] = provider

    # Step 5: 写入新节点 yaml
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        yaml.safe_dump(tmpl_data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Step 6: 更新调用者的 delegate_targets（如果指定了 caller_node_id）
    # 追加新节点到调用者的委派列表，使调用者可以 dispatch 到新节点
    caller_updated = False
    if caller_node_id:
        caller_path = workspace_root / "config" / "nodes" / f"{caller_node_id}.yaml"
        if not caller_path.exists():
            caller_path = workspace_root / "engine" / "system_nodes" / f"{caller_node_id}.yaml"
        if caller_path.exists():
            try:
                caller_data = yaml.safe_load(caller_path.read_text(encoding="utf-8"))
                if isinstance(caller_data, dict):
                    targets = caller_data.get("delegate_targets", [])
                    if not isinstance(targets, list):
                        targets = []
                    if name not in targets:
                        targets.append(name)
                        caller_data["delegate_targets"] = targets
                        caller_path.write_text(
                            yaml.safe_dump(caller_data, default_flow_style=False, allow_unicode=True),
                            encoding="utf-8",
                        )
                        caller_updated = True
            except Exception:
                pass  # 非致命：节点已创建但调用者更新失败

    # Step 7: 创建记忆 namespace 目录（mkdir -p）
    mem_dir = workspace_root / "data" / "memory" / memory_book
    mem_dir.mkdir(parents=True, exist_ok=True)

    return _ok(
        f"Agent created: {name}",
        node_id=name,
        config_path=f"config/nodes/{name}.yaml",
        memory_book=memory_book,
        memory_dir=f"data/memory/{memory_book}/",
        persistent=persistent,
        caller_updated=caller_updated,
        template=template,
    )


# ---------------------------------------------------------------------------
#  PLUGIN_META tool declarations
# ---------------------------------------------------------------------------
# [2026-05-28] 与 knowledge_inject.py 中 save_memory 等工具的注册方式一致：
# 在函数定义后将其引用填入 PLUGIN_META["tools"]，由 loader._register_declared_tools
# 注册到 ToolRegistry。
PLUGIN_META["tools"] = [
    {
        "name": "create_agent",
        "description": (
            "从现有节点 yaml 模板创建新的持久化子节点。"
            "读取模板节点配置，替换 id/name/description 等字段后写入 config/nodes/ 目录，"
            "并可选地将新节点追加到调用者的 delegate_targets 中。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "新节点 ID，如 bob",
                },
                "display_name": {
                    "type": "string",
                    "description": "显示名，不填则用 name",
                },
                "template": {
                    "type": "string",
                    "description": "模板节点 ID，如 child_coder。先查 config/nodes/，再查 engine/system_nodes/",
                },
                "description": {
                    "type": "string",
                    "description": "节点描述",
                },
                "memory_book": {
                    "type": "string",
                    "description": "记忆 namespace，不填则默认等于 name",
                },
                "persistent": {
                    "type": "boolean",
                    "description": "是否持久化，默认 true",
                },
                "provider": {
                    "type": "string",
                    "description": "覆盖 provider",
                },
                "caller_node_id": {
                    "type": "string",
                    "description": "调用者节点 ID，用于自动追加 delegate_targets",
                },
            },
            "required": ["name", "template"],
        },
        "func": create_agent,
    },
]
