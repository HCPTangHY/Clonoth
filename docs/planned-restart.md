# 计划重启与任务恢复

## 行为

设置页的 `/v1/admin/restart` 与 `request_restart` 使用同一流程。当前 `engine`、`all` 两种参数都执行 Supervisor 与 Engine 重启。

- 重启中断当前执行。正在执行的工具返回取消；不承诺撤销已经发生的外部副作用，不自动重跑原工具。
- 启动后恢复同一个 `task_id`，保留输入、会话代际、父子关系、批量等待、审批与原消息交付关联。恢复创建新的 `execution_id`，拒绝旧执行的迟到完成请求。
- 尚未领取的任务继续排队；等待子任务的父任务继续等待；中断的 AI 任务从持久历史继续推理。工具和脚本任务不能当作 AI 任务重放，而是通过原完成路由返回取消。
- 用户主动取消、暂停或重置会话的意图优先，不因计划重启而重新授权。
- 不写入“重启完成”的模型输入，不因此创建新任务。原后台工具、子任务的正常结果回调仍使用现有交付机制。
- 本机制不接收或缓存停机期间的新消息，也不提供进程崩溃后的自动恢复。

## 持久化与启动顺序

调度恢复记录位于 `data/restarts/pending.json`，使用原子替换保存：

1. `preparing`：生成 `restart_id`，停止领取新任务，保存调度状态，并通知插件准备停机。
2. Engine 取消在途操作，保存工具结果与执行状态，提交取消回执。
3. 确认 Engine 进程停止后，封存为 `ready`，然后退出 Supervisor。
4. 新 Supervisor 加载会话注册表，再恢复计划中的任务、交付索引和运行状态；恢复中的会话不参与孤立会话清理。
5. 保存恢复结果后，把记录置为 `consumed`。该记录不能再次授权恢复。

会话历史继续使用 `data/conversations/`。插件执行事实和证据继续使用 `data/executions/runtime.json`，不另建一份对话历史。

未封存、格式损坏或恢复写入失败时，不把未知状态当作成功。Engine 未确认执行检查点，或明确报告历史写入失败的任务，保持挂起并记录原因，不自动推理。应先检查存储、日志和已有副作用，再由用户决定后续处理。

## 工具记录

Engine 保留已经完成的调用结果，为被中断或尚未开始的同轮调用补取消结果。补全仅针对本任务的当前工具轮，并写回原位置，避免制造孤立工具消息或重复执行。

`request_restart` 的调用身份随计划保存。服务已接受请求但原回执未完整保存时，可按匹配的任务和调用 ID 补上“已接受”回执。正常接受后先保存回执，再等待停机，不进入下一轮模型请求。

后台工具使用原异步工作身份去重，沿现有 receipt 的排队、持久接收和处理完成流程结算。已有暂停回执保持暂存；原任务已结束时，按原后台结果路由寻找或创建结果接收任务，不用重启通知代替工具结果。取消记录明确说明：操作未返回确认结果，外部副作用可能已经发生，需要检查实际状态后再决定是否重试。

父任务已经获得但尚未写入历史的子任务或批量结果，也随调度记录保存。恢复输入使用独立身份确认是否已经持久接收，避免遗漏结果或重复插入。

## 插件与 Adapter

- 插件执行接口发布 `restart_preparing`；恢复后的 `ready` 携带 `planned_restart`、`restart_id`、恢复任务 ID 和停机时长。
- Goal 保留目标、预算、证据和在途评审，沿原任务继续；此前或准备停机后由用户暂停的 Goal 不自动重新授权。普通崩溃、插件重载或不匹配的重启标识仍要求明确恢复。
- SDK 仅在事件同时带有 `preserve_tasks: true` 与有效 `restart_id` 时保留交付关联。停机期间停止 typing 和超时清理；各 Worker 的首次注册使用同一批次标识。
- Discord 保留原触发消息、状态消息与日志消息，清除旧流式预览，恢复后继续更新。此处假设独立 Bot 进程仍存活，不包含 Bot 自身重启后的界面状态恢复。

## 离线验证

不需要启动真实服务或调用实际模型：

```bash
TMPDIR="$PWD/data/temp" python3.11 -B -m pytest \
  tests/test_restart_supervisor.py tests/test_restart_engine.py \
  tests/test_restart_integration.py tests/test_restart_delivery.py \
  tests/test_restart_goal.py tests/test_restart_api.py \
  tests/test_restart_async.py tests/test_restart_resume_input.py -q -p no:cacheprovider
```
