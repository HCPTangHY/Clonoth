# Clonoth 配置化生长面草案

本文档说明本轮新增的配置文件只是 **schema draft + bootstrap 示例**，目标是先把未来“可生长层”的结构定出来。

> 当前状态：Agent Profile、Prompt Pack、Model Routing 与 Workflow 选择已经开始被现有 runtime 消费。
> 但更完整的多 step internal workflow runtime 仍然是下一阶段目标。

---

## 1. 新增目录结构

```text
config/
├── agents/
│   ├── bootstrap.kernel_executor.yaml
│   ├── bootstrap.shell_orchestrator.yaml
│   └── bootstrap.task_responder.yaml
├── workflows/
│   └── bootstrap.default_chat.yaml
├── prompt_packs/
│   └── bootstrap_cn/
│       ├── manifest.yaml
│       └── fragments/
│           ├── core/
│           ├── roles/
│           └── styles/
└── model_routing.yaml
```

这些文件共同表达：

- Agent 是什么；
- Workflow 如何把多个 Agent 串起来；
- Prompt 如何被组合；
- 模型如何按 Agent / Role 被路由。

---

## 2. Agent Profile 草案

文件位置：`config/agents/*.yaml`

每个 profile 当前包含以下核心字段：

- `version`: schema 版本
- `kind`: 当前固定为 `agent_profile`
- `id`: 稳定标识
- `name`: 展示名
- `runtime`: `shell` 或 `kernel`
- `mode`: 该 profile 在 runtime 中承担的模式
- `description`: 角色说明
- `model_route`: 指向 `config/model_routing.yaml` 中的路由条目
- `prompt.pack`: 使用哪个 prompt pack
- `prompt.assembly`: 使用哪个 assembly
- `prompt.prefill`: 额外工作前缀/角色偏好（当前会被拼入最终 prompt）
- `context.injectors`: 希望 runtime 注入哪些上下文
- `capabilities`: 角色能力边界
- `output`: 输出通道与格式要求
- `compat`: 与当前 Python 实现的兼容映射

### 当前 bootstrap profiles

- `bootstrap.shell_orchestrator`
- `bootstrap.task_responder`
- `bootstrap.kernel_executor`

它们不是最终标准角色，只是 **兼容当前硬编码实现的默认 profile**。

---

## 3. Workflow 草案

文件位置：`config/workflows/*.yaml`

当前 workflow 草案表达：

- 从什么入口触发；
- 由哪些 step 构成；
- 每个 step 由哪个 runtime 承载、由哪个 agent profile 执行；
- step 之间如何传递输入输出；当前 runtime 已消费 `role=route|execute|finalize` 的 profile 绑定；
- 最终是否发出用户可见输出。

当前 bootstrap workflow：

- `bootstrap.default_chat`

它对应当前项目已有的兼容流程：

1. Shell 路由
2. Kernel 执行
3. Shell 最终总结

未来可以扩展出：

- planner -> executor -> reviewer
- router -> specialist A / specialist B
- brainstorm -> critique -> finalize
- 多模型投票 / fallback

---

## 4. Prompt Pack 草案

文件位置：`config/prompt_packs/**`

当前不再把 prompt 只看成单个 txt，而是拆成：

- `manifest.yaml`
- `fragments/core/*`
- `fragments/roles/*`
- `fragments/styles/*`

### 设计意图

#### core
放系统级约束：

- substrate 边界
- non-fabrication 约束
- tool protocol 摘要

#### roles
放角色特定职责：

- shell orchestrator
- task responder
- kernel executor

#### styles
放表达风格：

- concise_zh

#### assemblies
通过 `manifest.yaml` 中的 `assemblies` 定义，指定某个角色应拼哪些 fragments。

这意味着下一步实现 Prompt Assembler 后：

- 用户可以新增 fragments；
- 用户可以覆盖 assemblies；
- skill / workflow 也可以对 prompt 做 overlay；
- 同一个 runtime 可以承载多个不同 persona。

---

## 5. Model Routing 草案

文件位置：`config/model_routing.yaml`

当前 schema 的意图是：

- 不再默认全系统一个模型；
- 而是为每个角色定义一个可解析的 route；
- route 下允许存在候选 provider/model；
- 未来可支持 fallback、成本/质量权衡、任务类型分流。

当前示例路由：

- `shell_orchestrator_default`
- `task_responder_default`
- `kernel_executor_default`

目前它们都还以 OpenAI 为 bootstrap 默认路径，但结构已经为未来多模型、多 provider 打开了空间。

---

## 6. 与现有实现的关系

这些 schema 草案不是推翻当前代码，而是给现有实现提供迁移落点。

### 当前硬编码实现

- `shell.worker.orchestrate`
- `shell.worker.generate_task_final_reply`
- `kernel.worker.run_task`
- `config/prompts/*.txt`

### 迁移方向

未来可逐步改成：

- shell 已开始读取 `AgentProfile` 与 `Workflow` 来决定路由 profile / responder profile
- kernel 已开始读取 `Workflow` 与 task context 来决定 executor profile
- prompt 已开始由 `Prompt Assembler` 从 `prompt_packs` 动态拼装（失败时 fallback 到 legacy txt）
- model 已开始由 `Model Routing` 解析（当前仍以 OpenAI 路径为主）

### 兼容策略

当前采用的是“渐进接入”：

- 现有 Python 主流程仍然存在；
- 但 profile / prompt pack / workflow 已经开始成为运行时实际输入；
- 当配置缺失或不兼容时，系统会回退到 bootstrap 默认值或 legacy prompt 文件；
- 这样可以在不打断当前系统的情况下逐步完成从硬编码到配置驱动的迁移。

---

## 7. 下一步最推荐做什么

按优先级，我建议：

1. 实现 `AgentProfile Loader`
2. 实现 `Prompt Assembler`
3. 让 shell / kernel 能先读取 bootstrap profile
4. 之后再逐步接入 provider factory 与更丰富的 context / workflow 能力

原因很简单：

- 先把“角色定义权”从代码里抽出来；
- 再把“提示词拼接权”从单文件里抽出来；
- 最后再做更大的 workflow / 多 Agent 编排。

---

## 8. 一句话总结

本轮新增的这些文件，不是在增加一套新的写死结构；
而是在为 Clonoth 建立一个更底层的事实：

> **Shell / Kernel 是 runtime，Agent / Workflow / Prompt / Model 才是可生长层。**
