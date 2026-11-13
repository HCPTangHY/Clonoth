# 工具系统

本文说明 Clonoth 的工具调用模式、工具来源、审批机制和自修改能力。

## 工具调用模式

工具调用格式由节点配置中的 `tool_mode` 决定，回退到 `config/runtime.yaml` 中的全局配置。

### native

原生工具调用模式。工具定义通过 provider 的 tools 参数传入模型，模型返回结构化 `tool_calls`，结果以 `role=tool` 写回历史。适合支持原生 function calling 的 provider。

### fake-native

兼容模式。工具定义仍通过 tools 参数传入，但历史记录中的工具调用和结果会转换为文本形式。适合保留旧会话兼容性。

### json

文本内嵌模式。工具定义写入 system prompt，模型以 `<<<TOOL_CALL>>>` 标记输出调用。适合不支持原生工具调用的模型。

## 输出模式

节点配置中的 `output_mode` 决定回复方式：

- `hybrid`（默认）：纯文本作为隐式 finish，不要求调用 finish 工具。
- `tool_only`：必须通过 finish 工具提交回复，纯文本被拒绝并重试。

## 工具来源

### 内置工具

由 `toolbox/builtins/` 提供，在 `toolbox/registry.py` 中注册。当前内置工具：

| 工具 | 说明 |
|------|------|
| `list_dir` | 列出目录内容 |
| `read_file` | 读取文件；文本每次最多 500 行、64 KB，超过 10 MB 默认预览前 50 行，也可指定范围读取 |
| `write_file` | 写入文件 |
| `apply_diff` | 精确搜索替换；`start_line` 使用本批修改前的原文件行号，自动补偿前序修改 |
| `execute_command` | 执行 shell 命令，默认超时 90 秒，可通过配置或参数覆盖 |
| `grep` | 搜索或替换文件内容；支持正则、glob 和 content/files_only/count 输出模式 |
| `set_workspace` | 切换活跃工作区 |
| `manage_skill` | `list` 查看、`upsert` 创建或更新、`delete` 删除 Skill |
| `manage_memory` | `list` 查看、`upsert` 保存、`delete` 删除记忆；指定 ID 可读取完整条目 |
| `manage_mcp_client` | `list` 查看、`upsert` 创建或更新、`delete` 删除 MCP 客户端 |
| `manage_tool` | `upsert` 创建或更新外部脚本，`reload` 热重载；可声明 pure 和 async_mode |
| `request_restart` | 请求重启 Engine 或全系统 |
| `manage_schedule` | `list` 查看、`upsert` 创建或局部更新、`delete` 删除定时任务 |
| `get_context_window` | 查看当前上下文窗口使用情况 |

### 会话待办（IDE 插件）

`plugins/ide/` 注册 `read_todos` 和 `update_todos`。Todo 是持久工作记录，不负责调度任务，也不代表 Goal 已通过验收。

- 清单只绑定 session，不要求工作区名称或文件目录；切换工作区不切换清单。
- 主会话的临时入口分支共用所属主会话清单。普通子代理和 worker 使用自己的子会话清单；恢复或 accumulate 复用同一清单，新的 fresh/fork 子会话不复制父清单。
- 服务端根据任务记录解析归属，同步子任务优先使用 `input.child_session_id`，不能直接使用仍指向父会话的 `task.session_id`。
- `read_todos` 返回 `schema_version: 2`、`session_id`、`revision` 和 `items`。`update_todos` 使用 `expected_session_id`、`expected_revision`、`changes`、`remove_ids`、`order`，不再接受 `workspace_path`。`expected_session_id` 只核对当前清单身份，不允许选择其他会话；这也防止 fork 子任务误用父清单版本。
- 摘要在 `before_llm_call` 的请求副本末尾注入，读取当前任务所属清单；旧 Todo 摘要仅从请求副本中移除，不修改原始历史或 JSONL。空清单也提供创建所需身份和 revision。读取失败提示重新读取，不按空清单处理。
- Web 聊天顶部面板仍为只读，主、子会话均可显示自己的清单。

新清单位于 `data/ide/todos/sessions/<sha256(session_id)>.json`，Supervisor 在会话锁内执行身份校验、revision 比较和原子写入。首次读取无 v2 文件的 session 时，扫描旧目录根部，将唯一匹配的 v1 清单复制为 v2；事项 ID、状态、说明、revision 和时间记录保持不变，旧文件保留。多个旧清单对应同一 session 时返回 409，不自动合并。旧目录存在无法解析、无法确认归属的文件时，未迁移清单的读取会失败，需要管理员检查；已有 v2 清单不受影响。

重新启用不会放宽节点权限或工具审批。节点使用 allowlist 时需要包含对应工具；真实工具调用继续遵守部署中的 `data/policy.yaml` 和会话审批规则。默认未匹配的工具需要审批：主任务等待审批，未预授权的子任务会直接拒绝。因此部署前需要确认父会话可继承的审批规则是否已允许这两个工具；必要时由管理员仅为这两个工具配置允许，不改变其他工具的规则，也不通过插件绕过。部署需同步 IDE 插件和 `engine/attachments.py`，让 Supervisor 和 Engine 重新加载，并刷新 Web 页面；旧版本仅更新 Engine 或仅更新前端均不足以兼容新协议。

离线验证：

```bash
TMPDIR="$PWD/data/temp" python3.11 -B -m pytest tests/test_plugins_migration.py tests/test_todo_session_store.py tests/test_todo_engine.py
node --experimental-vm-modules tests/test_todo_frontend.cjs
```

### 会话 Goal（IDE 插件）

Goal 与 Todo 一样按 session 保存，但职责不同：Todo 是工作记录；Goal 在原执行及 output 链之外进行独立证据评审。主会话的入口 branch 使用同一 Goal，普通子会话不拥有父 Goal。工作区名称和目录不参与保存键，也不要求存在项目目录。

Web 临时查看会话不等于真实子会话。Goal 按后台返回的 `is_child` 判断权限：临时查看主会话时可管理该会话自己的 Goal，真实子会话不能管理父 Goal。入口 branch 显示所属主会话，读取响应中的 `requested_session_id` 对应当前查看会话，`session_id` 为规范 Goal 归属；提交对规范归属执行 CAS，输入框桥接仍校验当前查看会话。临时提交不创建普通侧栏会话，也不把当前临时会话绑定到已有侧栏条目。归属校验在后台进行，输入区不显示会话 ID、归属说明或检查过程；加载时禁用 Goal 按钮，失败时在按钮提供简短重试入口。不能在归属未确认时提交，草稿保持不变。

Web 输入框外侧、紧贴上边框的通用工具栏提供 Goal 按钮。点击打开右侧 Goal 面板并进入 Goal 输入状态：电脑直接在主输入框编写，手机在面板文本框编写并点击“创建并开始”或“修改并重新开始”。桌面侧栏打开期间始终按 Goal 提交，成功后不会自动切成普通聊天；关闭侧栏才恢复普通输入。手机运行中默认只看状态，需点击“修改目标”才显示编辑框。两端共用提交逻辑；也可在普通或 Goal 输入状态下使用：

```text
/goal 完成登录接口迁移，保持现有 API 兼容，相关测试和构建全部通过。
```

`/goal` 只表示本次提交目标，不是暂停、恢复或查询命令；`/goalkeeper` 不匹配。空目标不启动任务。前端去掉前缀后直接调用 Goal API，普通聊天后端不解析该命令，也不扫描历史、工具结果或子代理回调。Discord 提供独立扩展命令 `!goal 目标正文`，所有非 Bot 用户可发起，仅发起者可在 120 秒内点击 Embed 的确认按钮。只创建空目标或替换已完成目标，不覆盖未完成目标；不支持附件及其他管理命令。IDE 不存在、关闭或版本不支持时明确提示不可用，不转成普通聊天。创建权限不改变工具审批权限。OneBot 保持不变。

附件沿用现有上传接口，上传成功后将返回的落盘路径以文字加入目标。浏览器 `blob:` 地址不能作为附件路径。主 agent 需要通过读取工具查看文件，路径本身不代表已经读取，也不是验收证据。上传或提交失败保留草稿，不盲目更换请求身份重试。

Goal 的紧凑状态入口位于会话顶部，点击在右侧面板查看目标、验收条件与评审记录，暂停、恢复和清除操作也在该面板内。桌面面板不覆盖聊天正文；手机端复用现有侧栏，主动打开后显示，关闭后返回聊天。目标编辑两端共用主输入框草稿，手机面板文本框通过受控接口同步，评审设置默认折叠。关闭按钮、遮罩及滑动收起退出 Goal 输入模式并保留未提交正文，不暂停或取消已经开始的 Goal。Goal 按钮表示输入用途，不表示后台是否正在执行。轮次是预算使用情况，不是完成百分比。编辑目标保留开始编辑时的版本，其他客户端更新后需先核对，不能用提交前的新版本覆盖旧草稿。详情中的“保存设置并暂停”沿用仅保存 API，明确暂停目标，恢复时开启新一轮；运行中保存前需要确认。

- Web 提交目标即保存并开始：空目标或已完成目标按独立新建处理；更新未完成目标也明确授权开始，但预算不清零。API 仍保留仅保存的 `set` 和单独的 `start`、`resume` 操作。普通用户新消息和工作区变化暂停自动推进，不阻止正常聊天。
- 切换工作区保留同一份目标、Todo、预算和评审记录；切回不自动恢复。暂停不增加目标内容版本，也不因切换本身作废在途评审。
- 每轮回复正常展示。原 output 链结束后，相关后台工作必须完成结果投递、输入确认和承接处理，才进入评审。未通过使用正常 input 在同一 session 开启下一轮，不恢复旧 task、不使用普通 output reject 放行机制。
- 评审节点 `ide.goal_reviewer` 无工具、控制工具、技能及记忆权限，不能执行验证或修改成果。默认使用本轮实际 provider/model，可独立配置；仅有完成声明或 Todo 完成不能证明验收通过。
- 自动续轮最多 8 次，连续 3 次没有实质进展暂停；同一证据版本评审最多重试 1 次，单次 180 秒。等待相关执行超过 30 分钟暂停推进，但不取消后台工作。恢复不清零计数。
- 暂停期间保存已经返回的评审结果，不自动应用。恢复时检查目标版本和执行事实；目标修改、失败、取消或结果丢失不能默认为成功。
- 计划重启沿原任务恢复 Goal，保留预算、证据与用户暂停状态；非计划退出、插件卸载或重载后等待明确恢复。见[计划重启与任务恢复](planned-restart.md)。清除 Goal 删除专属记录和证据，不修改 Todo；会话重置/删除不把旧 Goal 转移到新 session。清除后的迟到回调不重新授权自动执行。

Goal 文件位于 `data/ide/goals/sessions/<sha256(session_id)>.json`。状态 revision、目标内容版本和执行代次分别校验。清除保留不含目标正文的版本/去重记录，避免旧请求覆盖新目标。API 为 `GET/POST /v1/plugins/ide/goal?session_id=...`；变更携带 `action`、`expected_session_id`、`expected_revision`、`request_id`，版本冲突后先读再改。`action="create"` 只接受 empty/completed，创建独立目标并授权启动；续轮与无进展次数、评审与专属记录从新目标开始，未传验收条件及评审模型配置时采用默认值。revision 和 goal_version 递增并保留幂等请求记录。替换先持久化新目标及待清理 ID，再清理旧专属证据；跨文件清理失败返回错误并受阻，不自动开始，重载可补做清理但仍需明确恢复。聊天历史与 Todo 不变。`action="submit"` 同时保存目标和授权启动，不需要客户端先 `set` 再 `start`，保留累计预算；未传入验收条件及评审模型配置时保留已有值。Web 用 create 新建、submit 修改未完成目标，Discord 只用 create。GET 返回只读 `capabilities.create`，供客户端识别能力。另提供 `goal_id`、`execution`（task_id、source_inbound_seq、started_at）及 `capabilities.progress`，供状态卡关联当前轮；未提交本轮或旧文件缺少关联信息时 execution 为 null。普通 set/submit 保留 goal_id，但清除旧执行关联；create/clear 改变身份。这些只读投影不会授权执行或在 GET 时写状态。无会话时先调用通用 `/v1/sessions/get_or_create`，不以普通消息创建占位任务。

第一轮不调用审查模型：明确开始后，程序将目标和验收条件组成普通 input 交给主节点。主节点及相关工作结束后才进行独立评审。API 不依赖主模型识别 `/goal`，各前端可自行设计交互。

评审输入按“固定目标和验收条件 → 其他任务 reply/finish → 本轮信息和最新主任务完整过程”排列。最新主任务取最终候选的 `entry_task_id`，不取最后完成的 QA 或转发任务。其他任务只保留已发生的 reply/finish 原文（包括附件信息），不按工具名称猜测哪些输出是测试或失败。主任务保留可见消息、完整工具调用与返回、已保存的工具 artifact 正文；去掉重复的存储及 provider 元数据，不要求无工具评审器自行读取路径。

旧输出按首次纳入顺序保存在 Goal 的 `_review_outputs` 中，迟到记录只追加，暂停、恢复与重启不改写既有内容。固定 JSON 字段顺序保持共同前缀，本轮状态和详细过程放末尾；目标修改后建立新版本前缀。每轮仍是独立评审任务，不累积全部旧工具过程。Anthropic 请求将固定区和每条旧输出分为独立文本块，在上一轮及本轮稳定区末尾设置缓存断点；其他提供方保持单条 JSON 的逐字前缀。缓存关闭时尊重提供方配置，实际命中仍取决于模型支持、最低长度与缓存有效期。

原始证据在相关任务工具 artifact 清理前有界采集：每任务 256,000 字节、每所属会话 2 MB、运行时合计 32 MB。reply/finish 独立保存，另有每所属会话 2 MB、合计 32 MB 上限，不受旧工具过程大小挤占。最新主任务记录缺失、调用返回不成对、相关输出记录缺失，或者完整输入超过容量时直接暂停；不发送截断 JSON，不让主节点因传输问题重复补证据，也不消耗评审重试或自动续轮次数。旧任务原始工具过程截断不影响已完整保存的 reply/finish。

评审默认模型保持现有顺序：显式 Goal 评审配置、评审节点配置、本轮主任务实际模型，最后回退全局默认。评审 YAML 不再固定 `context_limit`；省略或 null 时，每次准备评审都读取当前 `engine.compact.threshold_tokens`，与主推理循环共享预算及缺省回退值 100000。显式 `context_limit` 仍可覆盖；0 与全局阈值一样表示不作本地容量限制，其他值必须为大于 2048 的整数。这是 token 预算，不是模型声明的上下文硬上限，不自动查询模型容量。输入 JSON 与固定评审提示词复用引擎 `estimate_message_tokens` 估算，加 2048 token 预留；字节数单独记录，不再作为 token 数或保守上界。评审记录明确保存 `token_count_source=estimate`、估算输入和含预留的总量。主 session 的 usage 包含不同的提示词、历史与工具，不能通过差值精确计算独立评审输入；缓存 token 仍占上下文，不能扣除。估算可能与提供方实际计数不同，真实计数以本次请求返回的 usage 为准。需要更大容量时，应核对实际模型后修改预算，再明确恢复；仅更换模型不会自动改写全局预算。旧运行时记录没有独立输出事实时明确标记不完整，不把缺失内容解释为空。不可从已删除的输出恢复事实。工具返回被内联缩短时，依据结构化引用加入已保存的完整 artifact 正文；缺少正文即暂停。重复采集保留之前已保存的 artifact，避免原文件清理后再次采集丢失。reply 事件携带执行身份，传输未确认时在完成记录中标记，不把缺失回复视为完整历史。

运行边界：磁盘写入失败或部分路由结果不明确时，停止自动推进，不重复执行可能已交付的 output 链。目标保存和后续任务创建以持久状态及稳定请求身份连接，不是跨文件的单一磁盘事务；无法确认的阶段返回错误，需检查记录后处理，不盲目重复执行。旧协议缺少来源或投递身份时，不能作为 Goal 完成证明。关闭来源的精简身份和不确定请求暂不自动淘汰，以免迟到结果或旧请求重新执行；长期运行需要后续增加安全归档，不应直接删除未完成记录。插件与 Core 在同一受信任进程运行；执行能力接口不是不受信任插件的安全沙箱。

验证时明确指定 `tests` 目录，避免收集 `data/temp/` 下的历史副本：

```bash
mkdir -p data/temp
TMPDIR="$PWD/data/temp" PYTHONPYCACHEPREFIX="$PWD/data/temp/pycache" python3.11 -B -m pytest tests -q -p no:cacheprovider
node --experimental-vm-modules tests/test_goal_frontend.cjs
node --experimental-vm-modules tests/test_todo_frontend.cjs
```

### 伪工具（Pseudo Tools）

由 Engine 内部处理，不经过工具注册表：

| 伪工具 | 说明 |
|--------|------|
| `finish` | 提交最终回复 |
| `ask` | 向上游提问 |
| `intermediate_reply` | 发送中间进度消息 |
| `switch_node` | 切换到其他节点 |
| `compact_context` | 触发上下文压缩 |
| `manage_workers` | `list` 查看、`message` 追加指令、`preempt` 软中断、`cancel` 硬取消当前会话的下游任务 |

`preempt` 不带消息时请求任务停止，带 `message` 时按追加指令继续处理，与生产接口一致。`cancel` 可以指定任务或节点；省略目标时取消当前会话的活跃下游任务，但不取消调用任务本身。指定了不存在或不明确的目标时返回错误，不扩大取消范围。

### 自定义工具

位于 `tools/*.py`，以独立子进程运行。协议：stdin 读 JSON 参数，stdout 写 JSON 结果。通过 AST 解析 `SPEC` 字典注册，不 import 脚本代码。结构化 JSON 在文本截断前解析，避免较大的结果被截成无效 JSON。

脚本环境提供 `CLONOTH_WORKSPACE`（安装根目录）、`CLONOTH_SESSION_ID`、`CLONOTH_TASK_ID` 和 `CLONOTH_SUPERVISOR_URL`。管理脚本通过 `CLONOTH_ADMIN_TOKEN` 或本机 `data/.admin_token` 认证，不得将生产凭据写入脚本。

本次仅迁移 `clear_context.py` 和 `clonoth_debug.py` 两个管理脚本，其余本地脚本保持原状。清理上下文只应在用户明确要求时执行；脚本会排除当前调用任务，仍会终止其他相关任务。

统一管理工具不提供旧名称别名。节点工具名单应使用 `manage_tool`、`manage_schedule`、`manage_workers`、`manage_skill`、`manage_memory`、`manage_mcp_client`。已有部署还需检查自定义节点和策略文件中的旧工具名，不能只替换源码。

真实工具与控制工具按模型声明顺序处理。连续的显式异步工具可以分组启动，普通工具仍按顺序执行。中断时为未执行的原生工具调用补充结果，避免恢复后的请求出现配对错误。

### MCP 工具

MCP 客户端配置在 `data/mcp_clients.yaml`，支持 `stdio`、`sse`、`streamable_http` 三种传输。注册后工具名格式为 `mcp_{client_id}_{tool_name}`。

同一 Worker 事件循环中的节点启动和恢复会复用已有 MCP 桥及首次发现任务，不会重新删除工具或关闭 stdio 会话。配置变化由现有监视器按客户端刷新；显式卸载、代码重载或失效的桥才进行资源交接。桥、stdio 会话池和 schema 缓存由事件循环持有，完整重新导入插件包后仍可完成旧资源清理。首次部署这一状态存储变更需重启 Worker，不能仅热重载旧版本。

刷新仍执行工具发现；客户端配置、规范化工具定义及包装实现未变化时，保留原注册和函数身份，避免修改其他客户端导致待审批调用失效。实际定义或实现变化仍使旧审批失效。插件清理同时核对所属事件循环和 generation，不会因两个循环的 generation 相同而关闭另一个循环的桥。

## 审批机制

工具操作通过 `request_guard` 向 Supervisor 请求策略判定：

| 安全等级 | 行为 |
|----------|------|
| `auto` | 直接执行 |
| `approval_required` | 等待人类审批 |
| `deny` | 拒绝执行 |

### trust_level

路径操作会经过 `classify_path` 分类：

| trust_level | 含义 | 默认行为 |
|-------------|------|----------|
| `workspace` | 活跃工作区或 `data/` 目录 | 按策略规则，通常自动放行 |
| `trusted` | `workspace_root`（安装目录）或 `extra_roots` | 核心源码需审批 |
| `external` | 以上都不是 | 强制审批 |
| 空字符串 | 无路径操作（如 `execute_command`） | 回落到用户偏好 |

### 统一工具级审批

所有通过 `ToolRegistry.execute()` 执行的工具（内置、自定义、MCP）都会经过 Supervisor policy 的 `evaluate_tool` 检查。默认策略为 `approval_required`；已有内部文件或命令审批的工具可通过具体规则免除重复的工具级审批。可通过 `data/policy.yaml` 的 `tools` 配置覆盖。

审批只对应当次调用的工具实现、定义和参数。等待期间工具被移除，返回 `tool_unavailable`；实现或定义被替换，返回 `tool_changed`。这类情况是工具执行错误，不是假定用户拒绝，也不会自动重试或调用已卸载的旧实现。同步与异步预授权共用这项检查，调用结束或授权失败后清理临时授权记录。定义与参数按规范化 JSON 比较，区分 `1`、`1.0`、`true`，忽略对象键顺序；不能序列化的值以及 NaN、Infinity 在授权前返回 `tool_invalid_json`，授权期间出现则按变更拒绝。

### 文件保护规则

| 路径 | 策略 |
|------|------|
| `.env` | 硬拒绝 |
| `data/policy.yaml`、`data/events.jsonl` | 硬拒绝 |
| `engine/**`、`supervisor/**`、`toolbox/**`、`providers/**` | 需要审批 |
| `config/nodes/**`、`tools/**`、`data/config.yaml` | 需要审批 |
| `config/runtime.yaml` | 自动放行 |
| `data/` 子目录（attachments、memory、conversations） | 自动放行 |

### 命令硬拒绝

`rm -rf /`、`mkfs`、`shutdown`、`reboot` 等高风险命令模式被硬拒绝。

## 工作区

`set_workspace` 工具可以切换活跃工作区。切换后：

- `execute_command` 的 cwd 变为工作区路径
- `read_file`、`write_file` 等工具的路径解析基于工作区
- 工作区内路径的 `trust_level` 为 `workspace`
- 记忆按工作区隔离（`data/memory/{node_id}/@{workspace}/`）
- 工作区绑定到 session，跨 turn 持久化

工作区注册表保存在 `data/workspaces.yaml`，session 级绑定保存在 `data/sessions.json`。

## 自修改能力

AI 可以在人类审批下修改系统自身：

| 操作 | 途径 | 审批 |
|------|------|------|
| 创建工具 | `manage_tool(action="upsert")` | 需要 |
| 修改节点定义 | `write_file` → `config/nodes/*.yaml` | 需要 |
| 修改引擎源码 | `write_file` → `engine/**` | 需要 |
| 接入 MCP 服务 | `manage_mcp_client(action="upsert")` | 按配置文件写权限审批 |
| 调整运行参数 | `write_file` → `config/runtime.yaml` | 不需要 |
| 创建定时任务 | `manage_schedule(action="upsert")` | 需要 |
| 重启 | `request_restart` | 需要 |

安全策略（`data/policy.yaml`）和事件日志（`data/events.jsonl`）始终硬拒绝写入。`request_restart` 的 Git 检查点只提交已跟踪文件，不自动纳入未跟踪文件。
