## TODO LIST

<!-- LIMCODE_TODO_LIST_START -->
- [ ] 删除旧文件  `#delete-old`
- [ ] 新建 engine/ai_step.py — AI 节点单步执行  `#engine-ai-step`
- [ ] 新建 engine/context.py — 节点局部上下文  `#engine-context`
- [ ] 新建 engine/graph.py — workflow 图加载与遍历  `#engine-graph`
- [ ] 新建 engine/node.py — 节点定义加载  `#engine-node`
- [ ] 新建 engine/prompt.py — prompt 装载  `#engine-prompt`
- [ ] 新建 engine/protocol.py — 最小 handoff/completion 协议  `#engine-protocol`
- [ ] 新建 engine/runner.py — 统一执行器主循环  `#engine-runner`
- [ ] 新建 engine/tool_step.py — tool 节点执行  `#engine-tool-step`
- [ ] 统计最终行数  `#line-count`
- [ ] 更新 config/nodes/*.yaml，并入 prompt/model_route  `#update-config-nodes`
- [ ] 精简 config/workflows/*.yaml  `#update-config-workflows`
- [ ] 更新 shell/cli.py 和 main.py 接入新引擎  `#update-entrypoints`
- [ ] 新建验证脚本并通过  `#verify`
<!-- LIMCODE_TODO_LIST_END -->

# 节点化彻底重写方案

## 设计依据

严格按照 `docs/node_runtime_discussion.md` 第 14 节结论：

> 基础节点只分为 AI 节点和 tool 节点。
> AI 节点之间主要是平级接力关系。
> AI 节点与 tool 节点之间主要是父子调用关系。
> tool 节点负责与真实世界交互。

## 核心模型

### 两种节点

| 类型 | 职责 | 执行方式 |
|------|------|----------|
| AI 节点 | 理解、规划、分发、执行、总结 | 调用 LLM |
| tool 节点 | 与真实世界交互 | 直接执行确定性操作 |

### 两种关系

| 关系 | 说明 |
|------|------|
| AI → AI | 平级接力，workflow 定义 |
| AI → tool | 父子调用，AI 节点发起，tool 节点返回结果给父 AI |

### 执行流程

```
交互AI节点
  → AI节点（可分支出 tool 节点，tool 返回后继续）
  → AI节点（可分支出 tool 节点，tool 返回后继续）
  → ...
  → 交互AI节点
```

每个 AI 节点内部可以多次分支 tool 节点，每次 tool 返回后 AI 节点继续思考，直到产出 outcome。

## 新文件结构

### 保留不动
- `supervisor/**` — 状态、事件、API、策略
- `providers/**` — LLM 适配
- `kernel/registry.py` — 工具注册表
- `kernel/meta_tools.py` — 内置工具实现
- `kernel/mcp_runtime.py` — MCP 客户端
- `kernel/skills_runtime.py` — 技能发现
- `shell/cli.py` — CLI 薄适配（改为调用新引擎）
- `main.py` — 入口

### 删除
- `clonoth_profiles.py` — profile 层取消，字段并入 node
- `clonoth_handoff.py` — 旧 handoff 协议，重写
- `clonoth_node_runtime.py` — 旧节点运行时辅助，并入引擎
- `clonoth_workflows.py` — 旧 workflow 加载，重写
- `clonoth_nodes.py` — 旧节点加载，重写
- `shell/runtime.py` — 旧 shell 运行时
- `kernel/runtime.py` — 旧 kernel 运行时
- `kernel/prompts.py` — 旧 prompt 加载
- `config/agents/**` — 旧 profile 配置
- `scripts/verify_flows.py` — 旧验证脚本

### 新建
- `engine/graph.py` — workflow 图加载与遍历（纯图，约 80 行）
- `engine/node.py` — 节点定义加载（AI + tool，约 80 行）
- `engine/runner.py` — 统一执行器主循环（约 250 行）
- `engine/ai_step.py` — AI 节点单步执行（LLM 调用 + tool 分支循环，约 200 行）
- `engine/tool_step.py` — tool 节点执行（调用 registry，约 60 行）
- `engine/protocol.py` — handoff/completion 最小协议（约 40 行）
- `engine/prompt.py` — prompt 装载（从 node 定义读取，约 50 行）
- `engine/context.py` — 节点局部上下文（约 40 行）
- `engine/__init__.py`
- `scripts/verify_engine.py` — 新验证脚本

预计新引擎总计约 800 行。

### 配置变更
- `config/nodes/*.yaml` — 扩充字段，取消 profile_id 引用，直接包含 prompt/model_route/tool_access
- `config/workflows/*.yaml` — 保持现有图格式，去掉 runtime 字段
- `config/runtime.yaml` — 精简，去掉 shell/kernel 分区

## node 定义格式（新）

```yaml
version: 1
kind: node
id: bootstrap.kernel_executor
type: ai
name: 执行节点
description: 负责多步推理和工具调用
model_route: kernel_executor_default
prompt:
  pack: bootstrap_cn
  assembly: kernel_executor
tool_access:
  mode: all
output_mode: draft   # draft | reply | passthrough
```

type 只有两个值：`ai` 和 `tool`。

## workflow 格式（精简）

```yaml
version: 1
kind: workflow
id: bootstrap.default_chat
entry_node: orchestrator

nodes:
  orchestrator:
    on:
      reply: $reply
      handoff: executor

  executor:
    on:
      completed: responder
      failed: responder

  responder:
    on:
      reply: $reply
```

workflow 只描述 AI 主干接力。tool 分支不在 workflow 里出现，因为 tool 是 AI 节点的内部行为。

## AI 节点内部执行模型

```
AI 节点被调度
  → 组装 prompt + 上下文 + 指令
  → 循环：
      调用 LLM
      如果 LLM 返回 tool_call：
        执行 tool 节点（父子关系）
        把 tool 结果追加到消息历史
        继续循环
      如果 LLM 返回 outcome 选择：
        返回 outcome + payload
        退出循环
      如果 LLM 返回纯文本：
        作为 draft 输出
        返回 completed
        退出循环
  → 引擎根据 (node_id, outcome) 查 workflow 决定下一跳
```

## 模型不再选择 node_id

AI 节点不再调用 `handoff_to_node(node_id=...)`。

入口交互节点使用 `select_outcome` 工具：
```json
{"name": "select_outcome", "arguments": {"outcome": "handoff", "instruction": "..."}}
```

或者直接回复文本（outcome = reply）。

引擎根据 workflow 的 `on` 映射决定下一个 AI 节点。

## 执行顺序

1. 新建 `engine/` 目录，写出全部新引擎文件
2. 更新 `config/nodes/*.yaml`，并入 prompt/model_route
3. 精简 `config/workflows/*.yaml`
4. 更新 `shell/cli.py` 和 `main.py`，接入新引擎
5. 删除旧文件
6. 新建 `scripts/verify_engine.py`
7. 验证
8. 统计行数
