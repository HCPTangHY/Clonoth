## TODO LIST

<!-- LIMCODE_TODO_LIST_START -->
- [ ] 设计 Prompt Assembler 与 context injector 机制  `#assembler`
- [ ] 设计 AgentProfile Loader 与 bootstrap fallback 机制  `#loader`
- [ ] 补全配置生长面的 policy / rollback / healthcheck 方案  `#policy`
- [ ] 定义 Agent / Workflow / PromptPack / ModelRouting 的配置 schema 草案  `#schema`
- [ ] 设计 workflow/team runtime 与 task 绑定方式  `#workflow-runtime`
<!-- LIMCODE_TODO_LIST_END -->

# Clonoth Substrate VNext 实施计划

## 目标
将当前系统从“内置少量固定 Agent 的框架”演进为“提供稳定 Runtime、允许用户定义 Agent / Workflow / Prompt / Model Routing 的基座”。

核心思想：
- 固化协议与信任根，不固化角色与人格。
- Shell / Kernel 是运行时容器，不是唯一正确的 Agent 形态。
- 角色、提示词、上下文拼接、模型选择、协作方式应尽量数据化、版本化、可热更新。

## 非目标
- 不在第一阶段引入复杂 DAG 编排引擎。
- 不在第一阶段重写 Supervisor 的事件模型与审批协议。
- 不追求一次性做完整多 Agent 平台；先把“配置化生长面”抽出来。

## 设计边界
### 保持稳定的根
- Supervisor API 与 Event Schema
- Policy / Approval 语义
- Tool trace / artifact 协议
- Tool Registry 的 AST 安全边界
- Restart / rollback / healthcheck 机制

### 允许生长的层
- Agent profiles
- Prompt packs / prompt fragments
- Context injectors / context policies
- Workflow / team definitions
- Model routing / provider preference
- Skills 与其它用户定义行为模块

## 分阶段实施

### 阶段 1：定义配置对象，而不是继续内建角色
产出：
- `config/agents/*.yaml` schema 草案
- `config/workflows/*.yaml` schema 草案
- `config/prompt_packs/**` 目录约定
- `config/model_routing.yaml` 草案

要求：
- 每个配置对象必须有 `version` 与 `id`
- 每个对象都可被静态校验
- 每个对象都要支持默认值与向后兼容

### 阶段 2：Agent Profile Loader
产出：
- AgentProfile 数据结构
- 配置加载器与默认 profile fallback
- 当前 `shell_orchestrator` / `task_responder` / `kernel_executor` 迁移为默认 profile

要求：
- 现有行为在无自定义配置时保持兼容
- Profile 加载失败时自动回退到内置 bootstrap profile

### 阶段 3：Prompt Assembler
产出：
- Prompt fragment 机制
- Prompt pack manifest
- 变量注入与基础上下文拼接器

要求：
- 保留系统核心安全约束片段
- 将 persona/style/task/context 分层拼接
- 支持未来 skill/profile/workflow 覆盖 prompt 片段

### 阶段 4：Workflow / Team Runtime
产出：
- 任务级 workflow 定义
- 允许 Shell 为 task 选择 workflow/profile
- Kernel 根据 workflow 运行一个或多个 agent step

要求：
- 第一版只需支持线性步骤与条件分支
- 中间产物必须能落入事件流/artifacts，确保可审计与可恢复

### 阶段 5：Model Routing
产出：
- provider factory
- 按 agent / workflow / task_type 选择模型
- fallback / override 机制

要求：
- 现有 OpenAI 路径保持可用
- 为 Gemini / Anthropic 留出真实接入点
- 支持用户为不同角色设置不同模型偏好

### 阶段 6：风险控制与回滚
产出：
- 对 `agents/`, `workflows/`, `prompt_packs/`, `model_routing.yaml` 的 policy 分级
- 配置热加载失败时的 fallback 与错误事件
- 必要时增加“配置健康检查”

要求：
- 角色配置演化不能破坏 Root of Trust
- 高风险配置变更要能审批/回滚

## 当前代码迁移建议
1. 把 `shell/worker.py` 中对固定 prompt 文件和固定 OpenAIProvider 的依赖收敛到 profile loader / provider factory。
2. 把 `kernel/worker.py` 的固定 `load_kernel_system_prompt()` 演进为“读取 agent profile + assembled prompt”。
3. 将现有三个 prompt txt 视为 bootstrap 默认包，而不是最终结构。
4. 将当前固定的 Shell Orchestrator / Task Responder / Kernel Executor 明确标记为默认 profile。

## 验收标准
- 在不改 Python 代码的情况下，用户可以定义一个新的 agent profile 并被 Shell/Kernel 运行。
- 在不改 Python 代码的情况下，用户可以为不同任务选择不同 workflow。
- 在不改 Python 代码的情况下，用户可以为不同角色指定不同模型。
- 当配置损坏或不兼容时，系统能够回退到 bootstrap 默认配置。
- 所有运行中间态依旧可审计、可恢复、可审批。
