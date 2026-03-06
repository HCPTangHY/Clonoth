# Clonoth Substrate Manifesto

> 目标：把 Clonoth 明确定位为一个 **Agent Substrate（智能体基座）**，而不是一个预先定型的“完成品 Agent”。

---

## 1. 我们要做的是什么

Clonoth 要做的不是：

- 预先定义一套永远正确的 Shell / Kernel 角色；
- 预先内置一组固定 prompt、固定模型、固定协作结构；
- 用框架作者的偏好替代用户对未来 Agent 形态的定义权。

Clonoth 要做的是：

- 提供一个足够稳定、足够底层、足够可恢复的运行时；
- 提供清晰的协议、状态、审计、审批、回滚能力；
- 为上层角色、提示词、工作流、模型路由、技能系统留下自由生长空间；
- 让用户与系统本身都能在不破坏信任根的前提下，持续塑造新的 Agent 形态。

一句话：

> **Clonoth 不应只是一个 Agent。Clonoth 应该是能长出很多种 Agent 的底座。**

---

## 2. 根本原则

### 2.1 固化协议，不固化角色
必须尽量稳定的内容：

- Supervisor API
- Event Schema
- Task / Approval / Restart 语义
- Tool trace / artifact 协议
- Tool registry 与 AST 安全边界
- Policy / Approval 执行逻辑
- Rollback / healthcheck / process management 机制

不应写死在代码中的内容：

- Shell 的人格与话术
- Kernel 的唯一执行人格
- 是否必须由单一 agent 完成任务
- 是否必须只有一种 planner / coder / reviewer 结构
- 每类任务固定使用哪个模型
- 提示词如何组织与拼接

### 2.2 固化约束，不固化行为
Clonoth 的底层必须为行为设边界，但不应替行为定型。

固定的是：

- 不能伪造工具结果；
- 不能绕过审批；
- 不能破坏事件日志；
- 不能绕过 Supervisor 的策略判定；
- 不能让自进化直接侵蚀 Root of Trust。

可变化的是：

- 如何规划任务；
- 是否由多个专家协作；
- 提示词怎样拼接；
- 是否启用 review / critique / vote / fallback；
- 是否按任务、角色、项目、成本目标来动态选模型。

### 2.3 固化 Runtime，不固化 Persona
Shell 与 Kernel 应被视为运行时容器，而不是唯一的角色定义。

- **Shell** = 会话入口、任务编排、用户交互运行时
- **Kernel** = 执行循环、工具运行、能力协调运行时

它们可以承载默认角色，但不应与某个固定人格永久绑定。

> **Shell / Kernel 是 Runtime，不是 Persona。**

### 2.4 固化生长条件，不固化生长结果
Clonoth 应提供：

- 状态与记忆承载能力
- 能力注册与热加载能力
- 审计与回放能力
- 审批与回滚能力
- 配置、角色、技能、工作流的演化界面

但不应预先规定：

- 最终必须长成什么样的多 Agent 系统；
- 最优协作结构是什么；
- 用户必须接受哪种默认人格。

---

## 3. 对 Shell / Kernel 的重新定义

当前系统中，Shell 与 Kernel 已经承担了重要分工；但从长期看，它们应当进一步“去人格化”。

### 3.1 Shell 的长期定义
Shell 不应被理解为“固定的 Orchestrator Agent”。

Shell 的长期职责应是：

- 接收用户输入；
- 维护会话级状态；
- 决定本轮任务采用哪个 workflow / team / profile；
- 将任务交给下游执行 runtime；
- 在合适的时候选择一个 responder profile 输出最终结果。

这意味着：

- Shell 可以运行一个 router agent；
- 也可以运行一组可配置的分发规则；
- 甚至可以在不同会话里表现为不同人格与策略。

### 3.2 Kernel 的长期定义
Kernel 不应被理解为“固定的执行 Agent”。

Kernel 的长期职责应是：

- 运行任务级 workflow；
- 调用工具与记录 trace；
- 在多个 agent step 间传递中间产物；
- 在事件流和 artifacts 中保留可审计的执行足迹；
- 遇到敏感动作时回到 Supervisor 的约束之下。

这意味着：

- Kernel 可以执行单 Agent task；
- 也可以执行 planner -> executor -> reviewer 这样的多步链路；
- 还可以在未来承载“任务级混合专家”结构。

### 3.3 当前内置角色的定位
当前系统中的：

- Shell Orchestrator
- Task Responder
- Kernel Executor

应该被明确视为：

> **bootstrap defaults（启动期默认角色）**，而不是 Clonoth 的最终形态。

---

## 4. Clonoth 应开放的“生长面”

为了真正让系统后续自由生长，应把以下对象视为一等公民，并尽量数据化、版本化、可热更新。

### 4.1 Agent Profiles
一个 agent profile 至少描述：

- id / version
- name / display_name
- role / purpose
- provider / model preference
- prompt assembly 方式
- prefill / style / output constraints
- 能力边界（是否路由、是否用工具、是否总结、是否审查）

框架提供的是 **定义机制与加载机制**，而不是唯一的 profile 集合。

### 4.2 Workflow / Team Definitions
系统不应只支持单一路径：

- Shell 路由 -> Kernel 执行 -> Shell 总结

而应允许用户定义：

- 单 Agent 模式
- Router -> Specialist 模式
- Planner -> Executor -> Reviewer 模式
- Brainstorm -> Critique -> Finalize 模式
- 按任务类型选择不同 team 的模式

框架提供的是 **workflow runtime**，而不是唯一的工作流脚本。

### 4.3 Prompt Packs / Prompt Fragments
提示词不应长期停留在“3 个固定 txt 文件”的形态。

更理想的结构应支持：

- core safety fragments
- tool protocol fragments
- persona fragments
- style fragments
- task fragments
- project / skill overlays
- user-defined fragments

框架提供的是 **prompt assembly 机制**，而不是唯一 prompt 文案。

### 4.4 Context Injectors / Context Policies
并非每个任务都需要同样的上下文。系统应允许定义：

- 是否注入会话历史
- 是否注入 rolling summary
- 是否注入项目画像
- 是否注入活跃 skills
- 是否注入 policy 摘要
- 是否注入工具协议说明
- 是否注入 channel / user style 偏好

框架提供的是 **上下文拼接能力**，而不是固定拼接顺序。

### 4.5 Model Routing
模型选择不应只停留在“全局一个 model”。

系统应允许：

- 按 agent 选模型
- 按 workflow 选模型
- 按任务类型选模型
- 按成本 / 延迟 / 质量目标选模型
- 按失败策略进行 fallback

框架提供的是 **provider abstraction + routing surface**，而不是替用户预设唯一模型策略。

### 4.6 Skills 与其它用户定义行为模块
Skill 不应只被视为文档或附加说明。

长期来看，skill 应可以参与：

- prompt overlay
- context injection
- role specialization
- workflow selection
- project-specific behavior shaping

框架提供的是 **skill contract 与装载界面**，而不是限制 skill 只能作为静态资料存在。

---

## 5. Clonoth 必须守住的“不可自由生长区”

如果没有边界，自进化会退化为自毁。以下区域必须尽量稳定，或者至少受严格审批与版本控制：

### 5.1 Root of Trust
- Supervisor 作为信任根的角色
- Policy / Approval 的最终裁决权
- Process manager / rollback / watchdog
- 关键恢复逻辑

### 5.2 Execution Protocols
- 事件结构与状态机
- Tool trace 协议
- artifact 引用约定
- 审批请求/决策语义
- task 完成与失败报告语义

### 5.3 Safety Boundaries
- tool registry 的 AST 安全模型
- 受保护路径规则
- 敏感命令规则
- event log append-only 原则
- prompt / config / tool 高风险修改的审批边界

### 5.4 Compatibility Contracts
- provider adapter 接口
- agent/workflow 配置 schema 的版本语义
- 配置加载失败时的 fallback 机制

---

## 6. 未来的默认实现应该遵守什么

### 6.1 默认值只是启动器，不是世界观
Clonoth 可以带默认角色、默认 prompt、默认 workflow，方便开箱即用；但这些都应当被明确标记为：

- bootstrap default
- reference implementation
- example profile / example workflow

而不是唯一正统。

### 6.2 一切高层行为尽量数据化
只要不触碰 Root of Trust，就优先考虑：

- 配置表达，而不是代码表达；
- schema 表达，而不是硬编码表达；
- overlay / fragments，而不是单大 prompt 文件；
- profile / workflow 绑定，而不是 Python 分支逻辑。

### 6.3 热加载优先，重启其次
对于高层行为配置，应优先支持：

- 热加载
- 校验失败回退
- 版本化
- 审批后生效

尽量不要让“换一个人格 / workflow / prompt pack”变成必须改核心代码。

### 6.4 用户拥有形态定义权
Clonoth 的使用者应能逐步接管：

- 角色命名
- 提示词拼接
- 专家团队设计
- 模型路由策略
- 项目特定行为

而不是被迫接受框架作者为其决定的智能体形态。

---

## 7. 对当前阶段的指导意义

在现有代码基础上，这份 manifesto 给出的直接方向是：

1. **不要继续把新行为写死在 `shell/worker.py` 与 `kernel/worker.py` 里。**
2. **把当前固定 prompt 与固定角色，视为 bootstrap profile。**
3. **优先抽出 profile / workflow / prompt assembly / model routing 的配置界面。**
4. **继续保持 Supervisor、Policy、Event Sourcing、Tool Protocol 的稳定。**
5. **允许未来的创造性，发生在“生长层”，而不是 Root of Trust。**

---

## 8. 最终立场

Clonoth 的野心不应该是：

> “内置一个很强的默认 Agent。”

Clonoth 更应追求的是：

> **“成为一个能长出许多不同 Agent 形态的稳定基座。”**

也因此，Shell / Kernel 的未来不是越来越像“两个写死人格的大脑”，而是越来越像：

- 稳定的运行时
- 中立的容器
- 受约束但开放的生长环境

这才是自进化系统真正应该优先建设的方向。
