# 长任务等待与恢复

本地测试、构建和其他长命令由 Agent 执行，操作记录由 Hub 持久保存。`pending=true` 表示任务尚未完成；MCP 调用返回不代表测试结束，也不代表测试失败。已接受的操作不依赖 ChatGPT 持续轮询。

## 持续读取同一操作

提交后保留原 `operation_id` 和 `idempotency_key`，按照响应中的 `next_call.name`、`next_call.arguments` 继续调用。等待过程中不重新提交命令，不生成新的幂等键。任务进入终态后检查真实的结果、退出码和测试输出，再继续完成用户已授权的工作。

`operations_wait` 默认等待 10 秒，上限也是 10 秒；默认最多返回末尾 8000 个字符。完成通知会唤醒等待者，无需在等待期间反复查询完整操作记录。初次提交的等待窗口不因长命令而延长。

响应保留原有 `next`、`retry_after_seconds`，并增加以下可选字段：

- `elapsed_seconds`：操作创建到当前等待或已记录终态的累计秒数，包含排队时间，不是进程执行时长或预计剩余时间。
- `next_call`：下一次读取的工具名和参数。任务未完成时指向同一 ID 的 `operations_wait`；若请求省略了终态结果，则指向 `operations_get`；终态结果已返回时为 `null`。

等待结果只对已返回的日志设置 `after_output_seq`，减少重复输出。终态结果补读不携带这个游标，以便取回最终日志。`output_truncated=true` 仍表示输出不完整；需要更多日志时，可显式使用 `operations_get`，其默认上限仍为 131072 个字符。

公开 MCP 的 `next_call` 将内部 `operations_wait/get` 映射为只读 `task_query` 的 `wait/get`，`operation_ids` 保留原编号。这样等待和补读不会被引导到含取消、执行能力的混合 `process` 工具；内部面板 API 继续使用原方法名。

等待请求被取消或网络断开，不等同于 `operations_cancel`。停止操作必须显式请求取消；Agent 本身停止、命令超时和真实执行失败仍可能使任务结束。单次短等待与命令的 `timeout_seconds` 是独立限制。

## 连接状态与任务状态分开判断

面板 API 和原生 MCP 操作回执增加 `recovery`：`connection_state` 仅描述当前 Agent 连接，`task_state` 描述持久任务记录；`operation_id`、`after_output_seq` 保留原操作与输出位置。连接不可用不能把仍运行的任务改成失败。`task_outcome_confirmed=false` 的 interrupted/needs_review 也不能推断副作用已回滚。

`next_action=query_original_operation` 表示按现有 `next_call` 查询原编号；自动重放和另建操作建议始终为 false。`cancel_requested` 只是取消意图，须等待 Agent 确认。授权拒绝、撤权、能力禁用返回 `stop_and_review_permission`，不会提供“重试绕过”的建议。SHA 冲突需先重新读取当前版本，其他无原编号的未知结果先核实，不盲目重复。

面板连接/回执读取失败保留既有操作，提示继续查原编号；这既不停止 Agent，也不代表原任务失败。真实失败仍以终态和退出码为准。

## 排队范围、取消与撤权

核心文件读取、目录树和文件搜索按实际请求路径参与资源协调。等待某个产物目录的只读请求不会阻挡无关源码文件的写入；相同路径及父子路径仍保留互斥和公平排队。具名任务通过 `exec(task=...)` 调用时遵守本地任务策略：默认项目独占，显式 `allow_read_concurrency=true` 只允许并行读，调用者不能通过 resources 缩小该任务的本地约束。

批量读取中某项路径暂时无法安全定位时，会保守申请项目读锁，再逐项返回原有路径错误；这可避免该路径在等待期间变为可读后缺少资源协调。项目概览仍保留项目范围的读取协调。

已投递的只读文件/目录/搜索及项目概览请求允许保存取消意图。Agent 只对仍处于 accepted 的等待任务立即取消；已经开始的读取可能返回真实完成结果。写入已经开始后不能中断提交来伪造回滚，仍须等待回执，再按备份恢复。

Hub 会继续复核已投递操作的项目映射和授权。连接明确声明 `cancel_pending_protocol=1` 时，授权不再有效会发送仅取消未开始操作的消息并查询原回执；Agent 在同一事件循环中检查账本并设置持久取消标记，running、finishing 和已有终态均保留。消息丢失或重连后仍查询相同操作，不重放不确定的副作用。

旧 Agent 未声明该能力时，Hub 仍只查询原回执，不向其发送新的取消消息，也不声称撤权已经阻止旧版已接收的排队任务。已执行任务不会因授权变更被自动杀死；取消已运行进程仍需要明确的操作取消请求。源码更新不代表线上协议已启用，需在另行授权的升级后核对连接能力。

## ChatGPT 停止后恢复

本服务没有让已经结束的 ChatGPT 回合自动继续的机制。短等待、完成通知和 `next_call` 可以降低轮询开销、明确下一步，但不能保证 ChatGPT 会无限持续思考，也不能自动唤醒已停止的会话。

可在原会话中发送以下指令，并替换其中的操作 ID：

> 继续检查原 operation_id：`<原操作 ID>`。不要重新运行测试，不要换幂等键。读取原操作，按照 next_call 等到终态，检查退出码和输出，然后继续完成剩余任务。

若操作 ID 丢失，先使用 `operations_list`，根据原项目、工具和 `idempotency_key` 找回记录。`needs_review` 或 `interrupted` 需要检查实际本地状态，不能直接重跑不确定的写入或部署。

## 本地验证

在已安装开发依赖的虚拟环境中运行：

```sh
python3 -m pytest -q tests/test_coding_workflow.py tests/test_coding_integration.py tests/test_agentdock_workflows.py tests/test_reliability.py
python3 -m pytest -q tests/test_operation_continuation.py tests/test_operation_continuation_integration.py
python3 -m pytest -q tests/test_recovery_integration.py::test_actual_pytest_survives_running_hub_crash_without_restart
```

应同时验证：完整与 coding 工具目录的默认值一致；等待完成、超时和取消都释放等待者；同一操作支持并发等待；省略结果后能补读完整终态信息；断开客户端后本地任务仍可按原 ID 恢复且不重复执行。
