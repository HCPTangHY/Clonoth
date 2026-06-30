# RFC: Dream & Compact 架构重设计

> 基于 2026.4.19 讨论记录重建，交叉参考 Claude Code (CC) 源码实现。
> 最后更新: 2026-04-24

---

## 〇、背景与动机

当前 Clonoth 的上下文管理存在三个核心问题：

1. **Dream 没有信息源** — dream 节点只能对着 `list_memories` 的已有条目做静态审查，看不到对话历史、session 转录、代码库。没有「新信号」可以整理，所以每次整理结果都是「只 save 一些就结束了」。
2. **压缩粒度太粗** — 没有 task 级别的结构化边界，压缩器按消息数量 `keep_recent=6` 粗暴地切，切点可能落在 tool_use/tool_result 对中间，导致 API 校验失败或模型困惑。
3. **记忆提取太弱** — 单次 LLM 调用不共享 cache，无已有记忆感知，无互斥，无游标。

本 RFC 定义了从 CC 实现中提取的可移植改进方案，按优先级排列，每步可独立上线。

---

## 一、Dream 改进

### 1.1 CC 的 Dream 实现（参考）

**源码位置**: `services/autoDream/autoDream.ts`, `consolidationPrompt.ts`, `consolidationLock.ts`

- **存储**: 文件系统级，每个 topic 一个 md 文件（带 YAML frontmatter 分四类：user/feedback/project/reference），外加 `MEMORY.md` 索引文件（上限 200 行 / 25KB）
- **触发门控**: 三级递进
  - 时间门控：距上次整理 ≥24h（成本=一次 `stat`）
  - Session 门控：新 session 数 ≥5（成本=`readdir`+`stat`）
  - 锁门控：`.consolidate-lock` 文件的 mtime 就是上次整理时间，body 是 PID，失败时 mtime 回滚让下次可重试
- **Dream Prompt 四阶段**:
  - Phase 1 Orient：`ls` 记忆目录、读 `MEMORY.md` 索引、浏览现有 topic 文件（防重复）、检查 `logs/`
  - Phase 2 Gather：按优先级搜集新信号 — 日志文件 → 已漂移的记忆（与代码库现状矛盾）→ Session 转录搜索（grep JSONL，窄搜索）
  - Phase 3 Consolidate：合并入已有 topic（不建新重复）、相对日期转绝对日期、删被推翻的旧事实
  - Phase 4 Prune：更新索引、删过时指针、压缩过长行、解决矛盾
- **工具权限**: FileRead（无限制）、FileWrite/FileEdit（仅限记忆目录）、Grep/Glob（无限制）、Bash（只读命令：`ls`/`find`/`grep`/`cat`/`stat`/`wc`/`head`/`tail`）

### 1.2 我们的 Dream 现状

**配置**: `config/nodes/system.dream.yaml`

- **存储**: YAML book 文件（`people.yaml` / `rules.yaml` / `default.yaml` 等），无索引
- **触发**: cron 定时（UTC 19:00 / 北京凌晨3点），无 session 计数门控
- **工具**: 只有 `list_memories` / `save_memory` / `delete_memory` 三个
- **Prompt**: 三阶段（了解现状→识别问题→整理），比较泛泛
- **限制**: 单次最多操作 10 条

### 1.3 核心差距

| 维度 | CC | Clonoth |
|------|----|---------|
| 信息来源 | 对话转录(JSONL) + 代码库 + 日志 | ❌ 只有已有记忆条目 |
| Prompt 可操作性 | 具体的文件操作步骤 | 抽象的「找重复找矛盾」 |
| 工具集 | read_file + grep + 只读 bash | ❌ 无 |
| 锁/互斥 | .consolidate-lock + PID | ❌ 无 |
| 门控 | 时间 + session 计数 + 锁 | 仅 cron 定时 |

**根因**: dream 手里没有新信息，只能看着已有条目随便动动。

### 1.4 改进方案：三步递进

#### 第一步：给 Dream 开眼（最关键，最小改动最大收益）

改 `system.dream.yaml` 的 `tool_access`，加上 `read_file` 和 `execute_command`。改 prompt 让它：

1. `execute_command` 跑 `ls -lt data/conversations/` 看最近有哪些对话文件
2. `read_file` 读最近 3-5 个对话文件，获取「新信号」
3. `list_memories` 看已有记忆
4. 对比：哪些新信息还没存？哪些旧记忆被推翻了？

等于把 CC 的 Phase 1（Orient）和 Phase 2（Gather）搬过来。不改存储结构、不改调度。

#### 第二步：改 Prompt 为四阶段

照搬 CC 结构：
- Phase 1 Orient：`ls` 记忆文件 + 读对话记录
- Phase 2 Gather：从对话里提取新信号，用 grep 搜关键词
- Phase 3 Consolidate：合并进已有记忆（不是新建），相对日期→绝对日期，修正过时事实
- Phase 4 Prune：删冗余、合并重复、更新 keywords 使触发更精准

重点是 Phase 3 的「合并进已有，不要新建」。CC 的 prompt 明确写了 "merge into existing topic files rather than creating near-duplicates"。

#### 第三步：加门控和互斥

- **门控**: 在 scheduler 触发 dream 前检查，距上次整理 <24h 就跳过。用 `data/memory/.dream-lock` 文件记录上次时间
- **互斥**: 如果主节点在这轮对话里已经调了 `save_memory`，记忆提取器就跳过这个范围（防重复存）

### 1.5 Dream 触发门控设计

当前 session 数量 ≈ Discord 频道数，基本固定。Dream 不需要做「扫描哪些是新 session」的门控，直接按频道遍历。

- CC 的 session 门控（≥5 个新 session 才触发）对我们没意义 — 频道不会新增，变的是每个频道里的对话内容
- 触发条件改为：距上次整理 ≥24h + 任意频道有新对话（看 conversation 文件的 mtime）
- 读对话文件时直接 `ls -lt data/conversations/` 按修改时间排序，读最近活跃的几个频道
- 未来 Web/TUI 加进来后 session 会变多变杂，到时候再加 session 计数门控

### 1.6 不需要抄的部分

- CC 的文件系统级存储（每个 topic 一个 md）— 我们的 YAML book 结构够用，改了反而要重写所有工具
- CC 的 `MEMORY.md` 索引 — 我们的 `list_memories` 就是索引
- CC 的 forked agent 共享 prompt cache — 这是 Anthropic API 特有能力，我们用不了

---

## 二、记忆提取（extractMemories）改进

### 2.1 CC 的 extractMemories 实现（参考）

**源码位置**: `services/extractMemories/extractMemories.ts`, `prompts.ts`

每轮 assistant 响应后异步触发，作为 forked subagent 运行（共享主对话的 prompt cache，省 token）。核心机制：

- **预注入清单**: 启动前先扫记忆目录所有文件的 frontmatter，拼成「已有记忆清单」注入给 agent，避免浪费一轮在 `ls` 上
- **游标追踪**: 基于 message UUID 记录上次处理到哪，只处理新增消息
- **互斥**: 如果主 agent 这轮已经自己调了 `save_memory`，提取 agent 就跳过这个范围
- **轮数限制**: 最多 5 轮工具调用，防 rabbit hole
- **合并控制**: 已有 extraction 在跑时，后续请求排队（stash），跑完后做一次尾随提取
- **节流**: 可配置每 N 轮才触发一次（`tengu_bramble_lintel`，默认 1）

### 2.2 我们的 memory_extractor 现状

**配置**: `config/nodes/system.memory_extractor.yaml`

- 独立 LLM 调用（不共享 cache）
- 没有预注入已有记忆清单（会创建重复）
- 没有互斥（主 agent 存过的它可能再存一遍）
- 没有游标（靠消息计数 `min_increment=6` 粗略门控）
- 无 stash / 合并控制

### 2.3 改进方案

- **P3**: 加游标追踪 — 基于 session 的消息位置记录上次处理到哪，只处理新增消息
- **P3**: 加互斥 — 主节点这轮对话已调 `save_memory` 就跳过
- **P4**: 预注入已有记忆清单 — 启动 extractor 前调 `list_memories` 结果注入 instruction

---

## 三、前置基础设施：Task 结构化执行记录（P0）

### 3.1 问题

轮摘要、dream、extractor 都卡在「没有 task 级结构化文件可读」这个前提上。

当前 task 执行完后，痕迹只存在于：
- `data/conversations/` 里的 session context snapshot（给下一轮恢复上下文用，不是结构化 task 记录）
- `data/events.jsonl`（supervisor 级别的事件流，不是对话内容）
- `data/node_contexts/`（节点上下文快照，格式不适合扫描）

### 3.2 CC 的 Task 概念（参考）

**源码位置**: `Task.ts`, `tasks/DreamTask/DreamTask.ts`

CC 中 Task 是一等公民：
- `TaskStateBase`: id, type, status, description, startTime, endTime, outputFile, outputOffset
- `TaskType`: local_bash | local_agent | remote_agent | in_process_teammate | dream 等
- `TaskStatus`: pending | running | completed | failed | killed
- 每个 session 的完整对话有 JSONL 转录文件，dream 能 grep，extractor 能按 UUID 游标扫

### 3.3 方案：TaskRecord 数据结构 + JSONL 序列化

Task 完成时往固定目录写一份轻量 task transcript 文件（追加到 session 级的 JSONL），内容包括：

```python
@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    node_id: str
    start_time: str   # ISO 8601
    end_time: str
    user_input_summary: str     # 触发消息摘要
    tool_calls: list[dict]      # 工具名 + 关键参数，不含完整 result
    assistant_output: str       # AI 最终输出
    task_summary: str | None    # 轮摘要（如果已生成）
    step_count: int
    token_usage: dict           # input/output/cache tokens
```

存储路径: `data/transcripts/{session_id}/{task_id}.jsonl`

### 3.4 四个消费者

| 消费者 | 用法 |
|--------|------|
| 轮摘要生成器 | task 完成后生成 summary，写入 `TaskRecord.task_summary` |
| Dream | 扫 transcript 文件获取新信号 |
| 记忆提取器 | 按 task_id 做游标追踪 |
| 调试审计 | 回溯问题时查看完整执行记录 |

### 3.5 原子性保证

Task 是压缩、snip、轮摘要替换的**最小操作单元**。压缩永远不会切到 task 中间，不会破坏 tool_use/tool_result 配对。

---

## 四、压缩三级递进

### 4.1 CC 的压缩架构（参考）

CC 的压缩是多层递进的，每层都有独立的源码文件：

| 层级 | 文件 | 机制 | 触发条件 |
|------|------|------|----------|
| Microcompact（时间触发） | `microCompact.ts`, `timeBasedMCConfig.ts` | 距上次回复 >60min（cache 过期），内容清除旧 tool_result | gap ≥ 阈值分钟 |
| Microcompact（缓存编辑） | `microCompact.ts` (cached path) | 通过 API `cache_edits` 删除旧 tool_result，不破坏 cache | 工具调用数 ≥ 阈值 |
| API Microcompact | `apiMicrocompact.ts` | 服务端 `context_management.edits` 做 `clear_tool_uses` | 在 API 请求中声明 |
| Session Memory Compact | `sessionMemoryCompact.ts` | 用 session memory（增量摘要）替代全量 LLM 压缩 | autocompact 触发时优先尝试 |
| 全量 LLM Compact | `compact.ts` | forked agent 做全量总结，输出 9 段结构化摘要 | autocompact 触发 + SM-compact 不可用时 |
| Partial Compact | `compact.ts` (`partialCompactConversation`) | 用户手动选择消息范围，只压缩选中部分 | 手动触发 |

### 4.2 我们的压缩设计（三级）

#### Level 1: Microcompact（时间触发清理）

**对应 CC**: `timeBasedMCConfig.ts` 中的 time-based microcompact

- **触发**: 距上次 assistant 消息 ≥1h（server cache TTL 过期）
- **操作**: 内容清除旧 tool_result，保留最近 N 个。不走 LLM，纯文本替换
- **门控**: 工具调用次数 ≥3 才生效（纯闲聊跳过）
- **效果**: cache 反正已失效，趁机缩小重写体积

#### Level 2: 轮摘要替换

**前提**: 依赖 P0 Task Transcript

- **生成时机**: task 完成后，用轻量模型（如 gemini-3-flash）对该 task 的完整消息做一次 summarize，输出 200-500 token 摘要
- **门控**: task 内工具调用次数 ≥3 或 task 消息总 token ≥4K。纯闲聊跳过
- **存储**: 写入 `TaskRecord.task_summary` 字段
- **压缩时使用**: compact 触发时，先检查各 task 是否有 summary，有的就用 summary 替换原始消息
- **替换完还超限**: 进入 Level 3

#### Level 3: 全量 LLM 兜底

**对应 CC**: `compact.ts` 的 `compactConversation`

- **触发**: Level 2 替换完仍超限
- **操作**: dispatch `system.compactor` 做一次 LLM 压缩，输出结构化摘要
- **粒度**: 以 task 为原子单元，不会切断工具调用对

### 4.3 Level 0: Tool Result Budget（入口限流）

**对应 CC**: CC 有 `applyToolResultBudget()` 对单条 tool_result 做截断

**区分两个概念**:
- **Budget（入口限流）**: 工具返回结果时就做预算控制 — 模型当轮能看到完整内容做出正确判断，但下一轮这个结果在上下文里会被压缩/裁剪
- **Microcompact（事后清理）**: 存入上下文后，过一段时间再清除

**当前状态**: 我们现在 `read_file` 不做硬截断了（之前做过但太激进，浪费反复读取），只有真正的超大文件或二进制才限制。`search_in_files` 已加 50K 硬限流。

**优先级**: P7（当前已基本够用）。如果要做，应该是 budget 思路：工具返回结果时做预算控制，而不是先存完整版后面再改。注意不能破坏 prompt cache — CC 的做法是在首次发 API 前做限制，所以完整版从未被缓存过。

---

## 五、安全机制

### 5.1 熔断器（Circuit Breaker）

**对应 CC**: `autoCompact.ts` 中的 `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`

**问题**: 我们现在压缩失败后就是静默跳过（`_format_compact_summary` 返回空字符串，`apply_compact_summary` 不执行），下一轮又会触发 `should_compact`，又尝试压缩，又可能失败……无限循环。

**方案**: 连续 3 次压缩失败后暂停自动压缩，避免浪费 API 调用。几行代码的事。

```python
# 在 compact.py 或 runner.py 中维护
class AutoCompactState:
    consecutive_failures: int = 0
    MAX_FAILURES = 3
    
    def should_skip(self) -> bool:
        return self.consecutive_failures >= self.MAX_FAILURES
    
    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.MAX_FAILURES:
            log.warning("Circuit breaker tripped after %d failures", self.consecutive_failures)
    
    def record_success(self):
        self.consecutive_failures = 0
```

### 5.2 PTL Retry（压缩请求本身过长）

**对应 CC**: `compact.ts` 中的 `truncateHeadForPTLRetry`，`MAX_PTL_RETRIES = 3`

如果要压缩的上下文太长，连压缩请求本身都超了 LLM 限制 — CC 会自动截断最旧的 API-round groups 重试，最多 3 次。防止「太长了连压都压不了」的死锁。

**方案**: 在 compactor 调用前检查待压缩内容的估算 token 数，如果超过模型上限的 80%，从最旧的 task 开始丢弃直到安全范围内。

### 5.3 Reactive Compact（413 安全网）

**对应 CC**: `compactMessages.ts`（未在本次源码中读取，但讨论中提及）

API 返回 413（请求太长）时，从尾部逐步剥离 tool 调用轮重试。最后的兜底 — 万一前面所有压缩都没拦住。

**方案**: 在 LLM 调用错误处理中检测非 2xx 且响应体包含 `"too long"` / `"too large"` / `"max.*token"` / `"context.*length"` 之类的关键词，触发 reactive compact。不依赖具体 HTTP status code。

### 5.4 Snip Compact（中间段裁剪）

**对应 CC**: `grouping.ts` 的 `groupMessagesByApiRound` + 压缩逻辑中的选择性删除

CC 的 Layer 3 不是从头压到尾，而是选择性删除中间不重要的轮次，保留头尾。有了 task transcript 落盘后，snip 可以按 task 粒度操作 — 标记某些 task 为「已折叠」，加载上下文时跳过它们。

实现上在 session snapshot 里加一个 `snipped_task_ids: list` 字段。

---

## 六、优先级表

| 优先级 | 项目 | 复杂度 | 依赖 | 说明 |
|--------|------|--------|------|------|
| **P0** | Task 内核化（TaskRecord + 序列化/反序列化） | 中高 | 无 | 所有后续机制的前置基础设施。定义 TaskRecord 数据结构，task 完成时落盘 transcript，按 task 粒度加载历史 |
| **P1** | Microcompact（时间触发清理） | 低 | 无 | 距上次回复 ≥1h 时清除旧 tool_result。可与 P0 并行开发 |
| **P1.5** | 熔断器 | 极低 | 无 | 连续 3 次压缩失败暂停自动压缩。几行代码 |
| **P2** | Dream 开眼（加 read_file + execute_command） | 低 | 无 | 改 dream yaml 配置和 prompt，最小改动最大收益 |
| **P2.5** | Dream 四阶段 Prompt | 低 | P2 | 照搬 CC 结构，加 Phase 2 Gather 的具体步骤 |
| **P3** | 轮摘要生成 + 替换压缩 | 中 | P0 | task 完成后生成 summary，压缩时用 summary 替换原文 |
| **P3** | 记忆提取改进（游标+互斥） | 中 | 无 | 加 UUID 游标、主 agent 互斥 |
| **P4** | Dream 门控与互斥 | 低 | P2 | 时间门控 + lock 文件 |
| **P4** | 记忆提取预注入 | 低 | P3 | 预注入已有记忆清单 |
| **P5** | Reactive Compact（413 安全网） | 中 | 无 | API 返回过长时自动剥离重试 |
| **P5** | PTL Retry | 低 | 无 | 压缩请求本身过长时截断重试 |
| **P6** | Snip Compact | 中高 | P0 | 按 task 粒度选择性折叠中间段 |
| **P7** | Tool Result Budget | 低 | 无 | 入口限流。当前已基本够用 |

---

## 七、CC 源码参考索引

以下文件已在本 RFC 编写过程中阅读并参考（源码位于 `/www/wwwroot/CCsource/`）：

**压缩系统**:
- `services/compact/compact.ts` — 全量压缩主逻辑（forked agent、PTL retry、post-compact 文件恢复、skill 重注入）
- `services/compact/autoCompact.ts` — 自动压缩触发（阈值计算、熔断器、session memory 优先尝试）
- `services/compact/microCompact.ts` — 微压缩（时间触发 + 缓存编辑两条路径）
- `services/compact/apiMicrocompact.ts` — API 级上下文管理（`clear_tool_uses_20250919`、`clear_thinking_20251015`）
- `services/compact/sessionMemoryCompact.ts` — session memory 压缩（增量摘要替代全量 LLM）
- `services/compact/grouping.ts` — API round 分组（以 assistant message.id 为边界）
- `services/compact/prompt.ts` — 压缩 prompt（9 段结构化摘要 + `<analysis>` 草稿区 + no-tools 指令）
- `services/compact/postCompactCleanup.ts` — 压缩后清理（cache 重置、microcompact state 重置）
- `services/compact/timeBasedMCConfig.ts` — 时间触发配置（gap 阈值、keepRecent）

**Dream 系统**:
- `services/autoDream/autoDream.ts` — Dream 主逻辑（三级门控、forked agent、进度监控）
- `services/autoDream/consolidationPrompt.ts` — Dream prompt（四阶段：Orient→Gather→Consolidate→Prune）
- `services/autoDream/config.ts` — Dream 启用判断
- `services/autoDream/consolidationLock.ts` — 锁机制（mtime = lastConsolidatedAt，PID body，失败回滚）

**记忆提取**:
- `services/extractMemories/extractMemories.ts` — 提取主逻辑（游标追踪、互斥、stash+trailing、轮限制）
- `services/extractMemories/prompts.ts` — 提取 prompt（预注入清单、四类记忆、不保存规则）

**Task 与 Token**:
- `Task.ts` — Task 基础定义（TaskType、TaskStatus、TaskStateBase）
- `tasks/DreamTask/DreamTask.ts` — Dream Task 状态管理（phase、turns、filesTouched）
- `query/tokenBudget.ts` — Token 预算追踪（continuation 判断、diminishing returns 检测）

---

## 八、与 Session Conversation Store 设计的关系

`data/session_conversation_store_design.md` 中设计的 ConversationStore（JSONL 持久化、Message 数据模型、Formatter 三代兼容）是独立但互补的工作：

- **ConversationStore 解决**: session 级对话持久化、消息追加、压缩原地更新
- **本 RFC 解决**: task 级结构化记录、多级压缩策略、dream 信息源、记忆提取改进

两者共同作用点:
- Task Transcript（本 RFC P0）可以建立在 ConversationStore 之上 — 每条消息带 `task_id` 标签，transcript 是按 task_id 的视图
- 压缩操作（本 RFC L2/L3）直接作用于 ConversationStore，不再需要 `context_reset` 信号
- 压缩以 task 为原子单元（本 RFC 要求），ConversationStore 的 `compact()` 方法需要感知 task 边界
