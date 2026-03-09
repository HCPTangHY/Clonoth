## TODO LIST

<!-- LIMCODE_TODO_LIST_START -->
- [ ] supervisor/types.py — Task 类型  `#t1`
- [ ] engine/protocol.py — TaskResult  `#t2`
- [ ] engine/context_store.py — 上下文存储  `#t3`
- [ ] supervisor/state.py — task 调度  `#t4`
- [ ] supervisor/api.py — task API  `#t5`
- [ ] engine/context.py — RunContext  `#t6`
- [ ] engine/ai_step.py — yield 模型  `#t7`
- [ ] engine/runner.py — task worker  `#t8`
- [ ] shell/cli.py — 事件适配  `#t9`
- [ ] scripts/verify_engine.py — 测试更新  `#t10`
<!-- LIMCODE_TODO_LIST_END -->

# Task Runtime 重构方案

## 核心思想

所有节点间通信（包括工具调用）改为平铺的 task 序列。Supervisor 是唯一的调度者，根据 workflow 图决定下一个 task。节点不创建子任务，只产出 outcome。

## 数据模型

### Task
```
task_id, session_id, workflow_id, node_id
kind: ai_step | tool_exec
status: pending | running | completed | failed | cancelled
instruction, context_ref, resume_data, input_data
batch_id (tool 批次)
outcome, result_text, result_data, result_context_ref
```

### Resume Stack
每个 session 维护一个 resume 栈。节点 yield handoff 时 push 上下文，handoff 链完成时 pop 恢复。

### Tool Batch
一次 LLM 响应可能包含多个 tool_call。用 batch_id 关联，全部完成后创建 resume task。

## 调度规则

### 1. Inbound → 初始 task
Supervisor 收到 inbound 后自动创建 ai_step task（入口节点）。Engine 只轮询 task。

### 2. ai_step 完成后
- outcome=reply → 发 outbound
- outcome=yield_tool → 为每个 tool_call 创建 tool_exec task
- outcome=yield_handoff → push resume 栈，创建目标节点 task
- outcome=completed/failed → 查 workflow 边：
  - 目标是 $end → pop resume 栈恢复
  - 目标是 $reply → 发 outbound 或 pop 栈
  - 目标节点匹配栈顶 → pop 恢复
  - 否则 → 创建新 task
- outcome=cancelled → 结束

### 3. tool_exec 完成后
- 更新 batch 计数
- 全部完成 → 创建 resume ai_step task，注入工具结果

## 上下文持久化

### 保存时机
- AI 节点 yield（工具 / handoff）前

### 保存内容
- messages 数组、step 计数

### 恢复方式
- resume_data.type=tool_results → 格式化为 TOOL_TRACE 追加到 messages
- resume_data.type=handoff_result → 追加为 user message

## 取消机制

- cancel_session → 标记所有 pending task 为 cancelled
- running task 在检查点检测 cancel 标记
- 新 inbound 到达时自动取消旧 task

## 文件变更清单

1. supervisor/types.py — 增加 Task 类型
2. engine/protocol.py — TaskResult 替代 NodeOutcome
3. engine/context_store.py — 新文件，上下文保存/加载
4. supervisor/state.py — 增加 task 队列和调度逻辑
5. supervisor/api.py — 增加 task API，修改 inbound 处理
6. engine/context.py — RunContext 小改
7. engine/ai_step.py — 改为 yield 模型
8. engine/runner.py — worker 改为轮询 task
9. shell/cli.py — 事件展示适配
10. scripts/verify_engine.py — 更新测试
