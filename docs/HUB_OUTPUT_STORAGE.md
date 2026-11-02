# Hub 输出持久化与数据库排队观测

## 活动输出与历史记录

活动命令的输出仍是最多 131072 个字符的末尾快照。它不代表完整 stdout，也不能从 `output_seq` 推算字节偏移。新 Agent 发送完整快照和递增序号，旧 Agent 的追加片段继续按每条 8192 字符的原有上限处理。

Hub 将活动尾部保存到 `operation_output_tails`。父操作记录保留权威 `output_seq`、状态及时间戳，接收输出时不再把尾部文本放入包含请求、结果和授权字段的宽记录。重复、较旧或无效序号不修改游标和时间戳。接收阶段仅查询判断所需字段，不为每个输出消息读取请求密文或历史结果。

每份接受的快照都先提交 SQLite，随后发送输出可用通知。这里没有尚未落盘的合并缓冲。即使没有客户端持续轮询，Hub 重启后仍可按原操作 ID 读取已经提交的尾部。

终态事务依次完成：

1. 重新读取当前序号与尾部；若结果附带最终输出，以该输出为准，否则保留已经收到的尾部。
2. 保存终态结果、最终尾部和单调游标。
3. 删除该操作的活动尾部。
4. 保存对应完成事件 outbox。

任何一步失败都会回滚整个事务。完成提交之后，迟到的输出消息即使携带更大的序号，也不能覆盖 succeeded、failed 或 cancelled 的最终结果。needs_review 的恢复规则与原有协议一致。

## 兼容性与迁移

迁移只增加表和索引，不批量搬迁历史输出，不改用户、令牌、加密密钥或既有操作 ID。旧记录继续从 `operations.output` 读取。旧格式活动记录收到下一份输出时，才在同一事务内移入活动尾部表。完成后最终尾部回填到原历史字段，因此既有历史读取和备份流程保留原有结构。

数据库备份必须继续备份整个 SQLite 数据库及匹配的密钥，不能仅导出 `operations` 表。正在运行的新版本保存活动尾部后，旧 Hub 二进制不会理解新增尾部表；回退前应按既有发布回退流程停止接收新任务、等待活动操作完成并保存完整备份。不能把 schema 主版本未变化解释为运行中无条件降级安全。

公开读取会先进行当前操作、项目、凭据和工具权限检查，再读取活动尾部；活动表本身没有独立的公共查询入口。省略输出的状态读取仅查询活动尾部的字符数。游标只有在实际交付输出或确认同一游标未变化时才前进。

## 查询索引与观测

`operations_project_recent` 覆盖 Space、项目和时间范围，`operations_project_active` 支持同一授权范围内的活动计数；`operations_device_active` 用于设备级未完成任务检查。查询仍执行原有 owner、visibility、grant 等隐私条件，索引不缓存授权结论。

`Store.stats_snapshot()` 的内部统计分为三个阶段：

| 组 | 测量范围 | 用途 |
| --- | --- | --- |
| `worker.queue_ms` | 提交到单个 DB worker 开始运行 | 判断数据库阶段是否排队 |
| `worker.phase_ms` | 整个同步数据库工作阶段 | 找到包含解析、循环或事务的长阶段 |
| `lock.wait_ms`、`lock.held_ms` | 最外层 Store 锁等待和持有 | 区分线程争用与持锁工作；嵌套重入不重复计数 |
| `transactions.duration_ms` | 最外层 SQLite 事务到提交或回滚完成 | 判断写事务是否过长 |

统计只保留有上限的数字样本、计数和汇总，不保存 SQL、参数、项目名称、用户、路径、输出或错误正文。它们属于整个 Hub 进程的内部观测，不是某个租户或房间的业务统计。各组单独保持一致，不能把组间数值当作一次数据库事务的联合快照。

## 回归验证

```sh
python -m pytest -q tests/test_operation_output_storage.py \
  tests/test_operation_continuation.py tests/test_operation_completion_events.py \
  tests/test_store_concurrency_metrics.py tests/test_multidot_review_store_metrics.py \
  tests/test_optimization_store.py tests/test_audit_store.py tests/test_protocol_negotiation.py
```

测试使用临时数据库和合成凭据，覆盖旧 Agent 追加、快照跳号、重复消息、重启读取、终态竞争、outbox 失败回滚、撤权、输出游标、迁移保留及有界观测。查询计划测试比较同一组合成数据、同一授权条件加索引前后的结果与 SQLite 虚拟机工作量；该结果不能替代真实服务的吞吐与延迟压测。
