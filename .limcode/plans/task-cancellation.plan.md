## TODO LIST

<!-- LIMCODE_TODO_LIST_START -->
- [ ] supervisor/state.py: 取消状态存储和方法  `#cancel-1`
- [ ] supervisor/api.py: cancel + cancelled 端点  `#cancel-2`
- [ ] engine/context.py: check_cancelled 方法  `#cancel-3`
- [ ] engine/ai_step.py: 循环检查点  `#cancel-4`
- [ ] engine/runner.py: 子链检查点  `#cancel-5`
- [ ] shell/cli.py: Ctrl+C 捕获和取消请求  `#cancel-6`
<!-- LIMCODE_TODO_LIST_END -->

# 任务取消机制

## 设计

协作式取消：不强杀线程/进程，而是在执行循环的关键位置检查取消标记，提前退出。

无法中断正在进行中的 LLM HTTP 请求，但能在下一个检查点停下来。

## 改动

### 1. Supervisor 层

**supervisor/state.py**:
- 新增 `_cancelled_sessions: set[str]`
- 新增 `cancel_session(session_id)` 方法：写入 `cancel_requested` 事件，加入 set
- 新增 `is_cancelled(session_id) -> bool` 方法
- `rebuild_from_events` 中不回放 cancel（cancel 只对当次执行有效）

**supervisor/api.py**:
- 新增 `POST /v1/sessions/{session_id}/cancel`：调用 `state.cancel_session()`
- 新增 `GET /v1/sessions/{session_id}/cancelled`：返回 `{"cancelled": bool}`

### 2. Engine 层

**engine/context.py**:
- `RunContext` 新增 `async def check_cancelled(self) -> bool`：GET supervisor 的 cancelled 端点

**engine/ai_step.py**:
- `run_ai_node` 的 for 循环顶部：每步开始前 `if await rctx.check_cancelled(): return cancelled outcome`
- 工具执行前也检查一次

**engine/runner.py**:
- `_run_subchain` 的 for 循环顶部：每跳开始前检查
- `run_graph` 的 on_handoff 回调入口检查

### 3. CLI 层

**shell/cli.py**:
- 等待回复的 `while True` 循环中，把 `time.sleep(poll)` 改为可中断的等待
- 捕获 `KeyboardInterrupt`（Ctrl+C），发送 `POST /v1/sessions/{session_id}/cancel`
- 打印提示，跳出等待循环，回到 `you>` 提示符

### 4. 事件类型

- `cancel_requested`：用户发起取消
- `cancel_acknowledged`：engine 确认已取消（在返回 cancelled outcome 时 emit）

## 检查点位置

```
run_graph
  └── run_ai_node (入口节点)
        ├── [检查点] 每步循环顶部
        ├── LLM 调用（无法中断）
        ├── [检查点] 工具执行前
        ├── on_handoff 回调
        │     └── _run_subchain
        │           ├── [检查点] 每跳循环顶部
        │           └── run_ai_node (子节点)
        │                 ├── [检查点] 每步循环顶部
        │                 └── [检查点] 工具执行前
        └── ...
```
