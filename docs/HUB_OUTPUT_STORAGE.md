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

数据库备份必须保存整个 SQLite 数据库及匹配密钥，不能仅导出 `operations`。旧 Hub 二进制不识别 `operation_output_tails`。降级前应使用既有维护流程停止新准入、排空请求并停止 Hub，在一致的数据库或备份上确认 `SELECT count(*) FROM operation_output_tails` 的结果为 0，保存完整数据库及匹配密钥后再切换。

只检查 queued/running 等活动状态不足：unknown 或 needs_review 仍可能保有活动尾部。若计数非零，应继续使用当前版本核查并恢复原操作，不能删除尾部、伪造终态或直接运行旧代码。新版本已接受写入后，不得用升级前备份覆盖当前数据。schema 主版本未变化不代表运行中可无条件降级。

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

`worker` 统计覆盖普通阶段和内部控制阶段的总量。控制阶段用于认证 Agent 回包及发送锁内的最终授权检查，每通道 FIFO，最多八个连续控制阶段后让等待的普通阶段推进，仍只有一个执行线程。它缩短控制消息被普通排队阻塞的机会，不抢占正在运行的事务，也不为普通查询承诺固定时延。取消等待和数据库关闭都会收束已接纳阶段。

## 回归验证

```sh
python -m pytest -q tests/test_operation_output_storage.py \
  tests/test_operation_continuation.py tests/test_operation_completion_events.py \
  tests/test_store_concurrency_metrics.py tests/test_multidot_review_store_metrics.py \
  tests/test_optimization_store.py tests/test_audit_store.py tests/test_protocol_negotiation.py
```

测试使用临时数据库和合成凭据，覆盖旧 Agent 追加、快照跳号、重复消息、重启读取、终态竞争、outbox 失败回滚、撤权、输出游标、迁移保留及有界观测。查询计划测试比较同一组合成数据、同一授权条件加索引前后的结果与 SQLite 虚拟机工作量；该结果不能替代真实服务的吞吐与延迟压测。

## 冷操作负载归档（显式离线维护）

`operation_payload_archives` 在同一数据库保存压缩后加密的历史 result/output。
启动仅建表，不搬迁、不删除历史、也不执行 VACUUM。只有终态 succeeded/failed/cancelled
且 updated 超过 30 天、没有活动尾部和待清理媒体的操作可归档。ID、授权范围、幂等指纹、
状态、输出游标和时间戳均不改变，当前授权检查之后按原 ID 透明恢复读取。
单批最多 500 条，在同一事务内压缩、加密、读回验证并替换热字段；失败整体回滚。
压缩发生在加密之前，密钥轮换和缺失密钥检测包含归档密文。

先停止新准入、排空并停止 Hub，再执行（默认仅预览）：

```sh
python -m scripts.archive_operation_payloads --data /path/to/hub --batch 100
python -m scripts.archive_operation_payloads --data /path/to/hub --batch 100 --apply
```

每次 apply 先保存并验证完整 SQLite 备份及匹配密钥到状态目录的 `.payload-backups`，
不会自动清理备份；命令持有 Hub 与密钥维护锁，拒绝在线并发维护。按需重复有限批次。
同库归档保留普通全库备份恢复语义；不得仅备份 operations 表。

降级前用当前版本运行 `--restore --apply`，直到 operation_payload_archives 计数为 0，
同时确认 operation_output_tails 计数为 0，再保存完整当前数据库与匹配密钥。
不得用升级前的旧备份覆盖已经接受新写入的数据。需要回收数据库文件空间时，在另行授权的
维护窗口手动执行 VACUUM；归档命令从不自动 VACUUM。

管理员可用只读诊断 `python -m scripts.queue_contention_report --database /path/to/hub.sqlite3`。
该命令使用 mode=ro/query_only 的一致性读取，不加载命令、凭据或输出正文。
滚动 24 小时以 accepted 为分母，等待裁剪到窗口，按最后等待原因归类，阻塞链采用首个阻塞者；
报告车队比例、根阻塞者以及 >60 秒、单项目 >5、单根持有者 >5 的当前等待告警。
事件缺失或跨设备时间误差会影响归因，此报告不是精确的执行剖析。
