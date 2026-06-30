# ADR: Task-Session 架构决策

> 日期: 2026-04-25
> 状态: Accepted
> 参与者: Clonoth Contributors

## 背景

P0 Task 内核化需要决定 Task 和 Session 的数据归属关系。

## 考虑过的方案

### 方案 A：Task 粒度存储
- `data/tasks/{task_id}.jsonl` 每个 task 独立文件
- Session 退化为纯索引 `[task_id_1, task_id_2, ...]`
- 优点：干净的 ECS Entity 分离
- 缺点：加载上下文需要拼多个文件，I/O 开销大

### 方案 B：Session 存消息 + Task 标签（采纳）
- 消息存在 `data/conversations/{session_id}.jsonl`，每条 Message 带 `source_task_id` 标签
- Task 元数据索引存在 `data/transcripts/{session_id}.jsonl`
- Session 兼 Entity + Component（编排者 + 数据容器）
- 优点：与现有代码零冲突，load 一次全有，单 worker 无并发问题
- 缺点：非纯粹 ECS，消息交错风险（多 worker 场景）

### 方案 C：完整 ECS 框架
- 引入 Tick 驱动、World 容器等完整 ECS 机制
- 优点：极致解耦
- 缺点：Python asyncio 单进程下 Tick 无天然优势，重构成本极高

## 决策

采纳方案 B。理由：

1. **现实约束**：Python + 单文件 JSONL + 单 worker，文件级 task 分离是过度设计
2. **兼容性**：ConversationStore 不用改，Message 的 `source_task_id` 已在打标
3. **ECS 风格但不引框架**：TaskRecord 做纯数据结构，消费者各自独立扫描
4. **未来可迁移**：数据层做好分离，未来如果迁移到 Elixir/ECS 框架，数据可以直接搬

## 架构定义

- **Task = 纯 Entity**：只有 ID，通过 transcript 索引记录元数据（action、token 用量、工具计数），通过 `source_task_id` 关联消息链
- **Session = Entity + Component**：既是编排者（task 列表），也是消息的物理容器
- **消费者 = System**：Dream、Extractor、Compactor、轮摘要，各自独立扫描数据

## 已知隐患

1. **消息交错**：多 worker 并发写入同一 session 时物理顺序可能乱。当前单 worker 无此问题
2. **压缩归属**：summary 消息的 `source_task_id` 语义需明确（TaskRecord 有 `compressed` 标记）
3. **Session 膨胀**：长期运行的 session 文件越来越大，`replace_all` 全量重写。可通过 rotation 解决
4. **Child session 跨文件**：子节点消息在 child session JSONL，索引在 parent transcript。消费者需跟指针跳转

## 后续

- 等 Elixir 蜂群平台成熟后评估是否迁移
- 多 worker 场景下加文件写入锁
- Session 文件过大时实现 rotation 归档
