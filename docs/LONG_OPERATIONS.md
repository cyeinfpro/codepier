# 长任务等待与恢复

本地测试、构建和其他长命令由 Agent 执行，操作记录由 Hub 持久保存。`pending=true` 表示任务尚未完成；MCP 调用返回不代表测试结束，也不代表测试失败。已接受的操作不依赖 ChatGPT 持续轮询。

## 持续读取同一操作

提交后保留原 `operation_id` 和 `idempotency_key`，按照响应中的 `next_call.name`、`next_call.arguments` 继续调用。等待过程中不重新提交命令，不生成新的幂等键。任务进入终态后检查真实的结果、退出码和测试输出，再继续完成用户已授权的工作。

`operations_wait` 默认等待 10 秒，上限也是 10 秒；默认最多返回末尾 8000 个字符。完成通知会唤醒等待者，无需在等待期间反复查询完整操作记录。初次提交的等待窗口不因长命令而延长。

响应保留原有 `next`、`retry_after_seconds`，并增加以下可选字段：

- `elapsed_seconds`：操作创建到当前等待或已记录终态的累计秒数，包含排队时间，不是进程执行时长或预计剩余时间。
- `next_call`：下一次读取的工具名和参数。任务未完成时指向同一 ID 的 `operations_wait`；若请求省略了终态结果，则指向 `operations_get`；终态结果已返回时为 `null`。

等待结果只对已返回的日志设置 `after_output_seq`，减少重复输出。终态结果补读不携带这个游标，以便取回最终日志。`output_truncated=true` 仍表示输出不完整；需要更多日志时，可显式使用 `operations_get`，其默认上限仍为 131072 个字符。

等待请求被取消或网络断开，不等同于 `operations_cancel`。停止操作必须显式请求取消；Agent 本身停止、命令超时和真实执行失败仍可能使任务结束。单次短等待与命令的 `timeout_seconds` 是独立限制。

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
