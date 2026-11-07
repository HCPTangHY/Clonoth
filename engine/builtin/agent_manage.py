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
    # [2026-09-13] _worker 不再通过 nodes 声明提供：声明会压过 config/nodes/
    # 物理文件，导致设置页编辑写入文件后不生效。改为 ensure_worker_node_yaml
    # 落盘的 config/nodes/_worker.yaml 作为唯一来源，插件声明仅作为文件缺失时
    # 的默认内容模板。
}

# [2026-09-13] _worker 默认节点内容。字段与原插件声明一致；provider/model 留空
# 表示跟随全局默认，用户可在设置页节点编辑器中修改这两个字段。
WORKER_NODE_YAML = """id: _worker
type: ai
name: Temporary worker node
description: Anonymous ephemeral child node invoked automatically by spawn_worker. Adjust provider and model in the settings node editor to change its default model.
persistent: false
control_tools:
  - finish
  - intermediate_reply
  - compact_context
  - manage_workers
tool_access:
  mode: all
  deny:
  - manage_memory:!list
prompt: |
  You are a capable assistant running inside a multi-agent system. Complete the task below, then submit your result.

  {{instruction}}
model: ""
provider: ""
delegate_targets: []
"""


def ensure_worker_node_yaml(workspace_root: Path) -> Path:
    """在指定工作区落盘默认 _worker 节点文件（存在即跳过，返回文件路径）。

    [2026-09-13] Why: _worker 原为插件声明节点，admin 节点编辑器会解析到插件
    Python 源文件，无法在设置页修改 provider/model。How: 改为 config/nodes/
    _worker.yaml 物理文件作为唯一来源，移除插件声明；首次使用前由本函数生成
    默认内容。Purpose: 前端节点设置页可直接编辑临时子节点的渠道与模型。
    """
    nodes_dir = Path(workspace_root) / "config" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    target = nodes_dir / "_worker.yaml"
    if not target.exists():
        target.write_text(WORKER_NODE_YAML, encoding="utf-8")
    return target


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


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: create_agent validation failures should be readable
    # through data.result. How: wrap the error once. Purpose: avoid legacy plugin
    # error payloads during migration.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


def _append_delegate_target_preserving_format(raw_yaml: str, target_name: str) -> str | None:
    """Insert ``- {target_name}`` under ``delegate_targets:`` without re-dumping the whole YAML."""
    lines = raw_yaml.splitlines()
    for idx, line in enumerate(lines):
        m = re.match(r"^(\s*)delegate_targets\s*:\s*(\[\s*\])?\s*(#.*)?$", line)
        if not m:
            continue
        indent = m.group(1)
        is_empty_inline = bool(m.group(2))
        comment = f" {m.group(3)}" if m.group(3) else ""
        item_indent = indent + "  "
        if is_empty_inline:
            lines[idx] = f"{indent}delegate_targets:{comment}"
            lines.insert(idx + 1, f"{item_indent}- {target_name}")
            return "\n".join(lines) + ("\n" if raw_yaml.endswith("\n") else "")
        # Find existing list items under delegate_targets:
        insert_at = idx + 1
        while insert_at < len(lines):
            cur = lines[insert_at]
            stripped = cur.strip()
            if not stripped or stripped.startswith("#"):
                insert_at += 1
                continue
            item_m = re.match(r"^(\s*)-\s+(\S+)", cur)
            if item_m and len(item_m.group(1)) >= len(indent):
                item_indent = item_m.group(1)
                insert_at += 1
                continue
            break
        lines.insert(insert_at, f"{item_indent}- {target_name}")
        return "\n".join(lines) + ("\n" if raw_yaml.endswith("\n") else "")
    return None


# ---------------------------------------------------------------------------
#  create_agent tool
# ---------------------------------------------------------------------------

async def create_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a new persistent sub-node from an existing node yaml template.

    [2026-05-28] 工作流程：
    1. 读取模板节点（先 config/nodes/ 文件，再插件声明）
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

    # Step 2: 查找模板（先 config/nodes/ 文件，再插件声明）
    tmpl_path = workspace_root / "config" / "nodes" / f"{template}.yaml"
    tmpl_data: dict | None = None
    if tmpl_path.exists():
        try:
            tmpl_data = yaml.safe_load(tmpl_path.read_text(encoding="utf-8"))
        except Exception as e:
            return _err(f"failed to parse template: {e}")
    else:
        from engine.faces.nodes import declared_node
        tmpl_data = declared_node(template)
    if not isinstance(tmpl_data, dict):
        return _err(f"template not found: {template} (checked config/nodes/ and plugin declarations)")

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

    # Step 5: 审批守卫 + 写入新节点 yaml
    from toolbox._common import request_guard

    _op, err = await request_guard(
        ctx,
        "write_file",
        {"path": f"config/nodes/{name}.yaml", "tool_name": "create_agent", "template": template},
    )
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

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
        if caller_path.exists():
            try:
                raw_caller_text = caller_path.read_text(encoding="utf-8")
                caller_data = yaml.safe_load(raw_caller_text)
                if isinstance(caller_data, dict):
                    targets = caller_data.get("delegate_targets", [])
                    if not isinstance(targets, list):
                        targets = []
                    if name not in targets:
                        patched_text = _append_delegate_target_preserving_format(raw_caller_text, name)
                        if patched_text is not None:
                            caller_path.write_text(patched_text, encoding="utf-8")
                        else:
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
            "Create a new persistent child node from an existing node yaml template. "
            "Reads the template node config, replaces fields such as id/name/description, "
            "writes it into config/nodes/, and optionally appends the new node to the "
            "caller's delegate_targets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "New node ID, e.g. bob",
                },
                "display_name": {
                    "type": "string",
                    "description": "Display name; defaults to name",
                },
                "template": {
                    "type": "string",
                    "description": "Template node ID, e.g. child_coder. Searches config/nodes/ first, then plugin declarations",
                },
                "description": {
                    "type": "string",
                    "description": "Node description",
                },
                "memory_book": {
                    "type": "string",
                    "description": "Memory namespace; defaults to name",
                },
                "persistent": {
                    "type": "boolean",
                    "description": "Whether to persist; default true",
                },
                "provider": {
                    "type": "string",
                    "description": "Override provider",
                },
                "caller_node_id": {
                    "type": "string",
                    "description": "Caller node ID, used to auto-append delegate_targets",
                },
            },
            "required": ["name", "template"],
        },
        "func": create_agent,
    },
]
