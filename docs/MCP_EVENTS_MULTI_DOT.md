# MCP Events 与多 Dot 接入、恢复及现场验收

本指南适用于包含 v2 统一收件箱和消费会话的代码。版本号相同不代表源码相同：上线前核对审核通过的候选提交、完整源码包 SHA-256、运行镜像与生成资源。测试消费者、回环压测和浏览器模拟的结果分别记录；实际宿主收发和物理手机体验需要各自的现场回执。

## 接入前检查

1. 确认服务运行的是本次候选，存在 `hub/collaboration/dot_mailbox.py`、`dot_consumers.py`、`team_collaboration.py` 和 `relay_hints.py`，实际工具目录包含新增动作。
2. 由房主在当前有效房间明确批准每名 Dot 的项目、能力、期限和预算。旧房间归档、位置过期或授权撤销时，按当前宿主流程重新接入；不要复用旧票据或静默恢复授权。
3. 使用面板生成的完整加入指令。核对 `dot_connection` 返回的项目、会话、Dot 与批准范围。仅刷新目录或查询连接不会建立原生订阅。
4. 区分独立授权与同 grant 的逻辑路由。同 grant 的两个 Dot 不具有独立凭据隔离；宿主不能可靠区分回调时，使用独立绑定或限制执行成员。

接入基础规范见 [DOT_COLLABORATION.md](DOT_COLLABORATION.md) 和 [DOT_RELAY_EXPERIENCE.md](DOT_RELAY_EXPERIENCE.md)。

## 协商稳定的 v2 订阅

已明确获准双向通信的 Dot 调用 `collaboration(action="dot_mailbox_configure")`，提供当前 `dot_id`、`protocol_version=2`、`confirm_consumer_protocol=true` 和稳定幂等键，以及当前工具模式要求的 scope。

配置进入 prepared 后，由实际宿主按返回的精确 `subscription_request` 建立 `codepier.collaboration.inbox_dirty.v2` 原生订阅。保留项目、环境、会话、slot 和 binding_generation 的精确过滤条件。callback、签名材料与连接器标识由宿主生成；不要从插件名称推测。

原生 challenge 成功后才算激活。每名 Dot 保留一条稳定订阅，Goal 与工作变化通过 Inbox 引用承载。普通测试事件不发消费票据；webhook 返回 2xx 只表示回调传输被接受。

## 宿主每次唤醒的处理顺序

1. 验证回调签名并按原投递标识去重，在宿主私有持久状态中保存本次 `consumer_begin` 的完整请求和幂等键，再发送请求。票据、proof 和签名密钥不得进入聊天或公开证据。
2. 调用 `collaboration(action="consumer_begin")`，使用回调提供的票据、当前 binding_generation、稳定 consumer_run_id 与 idempotency_key。回包丢失时重放完全相同的 begin 请求。换键重试同一票据会被视为另一消费回合。
3. 核对回包中的 session 状态和 lease_until。幂等回放可能返回已经过期的原会话；取得原 proof 不等于租期仍有效。业务步骤仍须现有工作授权与资源门禁。
4. 使用 `collaboration_query` 的 `dot_mailbox` 读取有界页面，继续请求优先采用响应中的精确 next_page_request。读取不确认消费、不续租。按实际工具模式携带当前 session/fence/proof 推进写入，长回合使用 `consumer_heartbeat`；消费租期与工作租期分别管理。已处理的 peer_handoff、result_ready、control_notice 使用 `dot_mailbox_ack` 确认本 Dot 的精确 entry_ids；用户输入和工作必须走原处理流程，不能用 ack 冒充回复或工作完成。
5. 新输入改变 revision 时，重读并协调新意图后再继续推进。`DOT_INBOX_CHANGED` 不表示输入丢失；不要把旧结果强行覆盖当前输入。每条来源输入都须保留明确处理关系。
6. 排空后用精确 expected_inbox_revision 调用 `consumer_yield`；若期间再次变脏，继续读取处理。重启后先查询原消费、工作和 operation，再决定是否恢复；不要重新派发同一副作用。

激活 v2 后，原旧写入口也需要消费凭证。订阅失效时不能自动降回无 fence 的写入路径。普通查询 `dot_consumer` 不返回票据或 proof。

## 多 Dot 团队

每名成员先独立完成上述闭环。房主在面板批准团队、唯一主责、成员、项目、能力与总预算。普通多 @ 或成员间发言不能扩大批准范围。

`collaboration_work(action="team_from_message")` 复用房主所选来源的唯一 Goal。交接使用 `collaboration(action="team_handoff")`，保留指定 assignee、工作关系与当前版本；工作推进需要当前 coordinator epoch、intent revision 和执行 Dot 的消费凭证。成员产物使用确切版本和摘要，最终由当前主责通过 `team_publish` 汇总。主责接管使用 `team_takeover` 的当前条件，旧主责不能继续发表。

先在获准的只读测试项目验证：一条房主输入、一个 Goal、A 交接 B、B 返回指定版本、A 发表唯一汇总。不要用两个独立聊天各自回答作为团队协作证明。

## 上线后的五类真实验收

所有故障注入在已批准的隔离环境进行。重启生产服务、发真实消息、创建订阅和变更授权按当次明确范围执行。每次验收绑定同一候选提交和源码包摘要，记录 Hub、Agent 和实际宿主版本及带时区的起止时间。

| 场景 ID | 最小现场步骤 | 核对结果 |
| --- | --- | --- |
| duplex | 房主发一条输入；真实宿主收到签名 callback、begin、读 Inbox、回复原话题；再补一条输入 | 原输入分别保留并有处理关系；记录原生订阅、回调、消费和原话题回复，不只记录 HTTP 200 |
| authorized_work | 在预先批准项目中执行有回执的可恢复操作；人为丢弃一次操作回包，再查询原 operation | 复用原 operation ID；实际副作用计数为 1；阶段回复与最终结果回原话题 |
| team | A 接 Goal、交接 B、B 回指定版本产物、A 汇总；再验证旧主责或过时 intent 不能发表 | 两名真实消费者、同一 Goal、明确 assignee、handoff、产物版本与唯一 publication；过时提交被拒 |
| recovery | 分别丢弃 begin 回包、断开宿主网络、重启测试 Hub，按原标识恢复 | begin 原参数重放返回原 session/fence/proof且不延长 lease；工作与 operation 不重建；不重复副作用或 publication |
| subscription | 实际续订一次；跨过期窗口检查，再明确恢复原绑定；最后暂停或撤权 | 原生刷新回执、过期停止投递、撤权阻断新准入、原 Inbox 保留；不静默续权或复制到新房间 |

在上述场景中额外逐项收集以下负面回执，避免只证明顺利路径：

- **重复回调**：同一已接收投递再次到达，宿主复用原 begin 请求。原回包丢失可恢复；改 consumer_run_id/幂等键重兑已使用票据应收到 `DOT_WAKE_TICKET_REDEEMED`。不得复制执行或发表。
- **未兑换票据过期**：保留一张尚未兑换的票据，真实等待其到期后调用 begin，应收到 `DOT_WAKE_TICKET_EXPIRED`。原业务输入仍在；根据当前订阅和绑定流程恢复后处理。不要将“已兑换 begin 原参数幂等回放”混作未兑换过期测试。
- **旧消费者迟到**：让旧租期结束，当前回合取得新 fence 后，旧回合尝试写入，应被拒；正确旧凭证但租期结束通常为 `DOT_CONSUMER_LEASE_EXPIRED`，篡改或跨 Dot 凭证为 `DOT_CONSUMER_PROOF_REQUIRED`。精确错误按实际拒绝层记录，不为匹配某码绕过前置门禁。
- **排空竞态**：读取后、yield 前插入新输入，旧 revision 的结束请求不得清除新输入。按当前响应重读并排空；新旧输入均有实际处理去向。
- **身份边界**：A 不能用自己的 session/proof 写 B；B 不能以另一 run 兑换 A 已使用票据。保留匿名 Dot、grant/绑定关系，独立核对两名参与者没有被同一身份别名冒充。

物理手机另验中文输入法、输入期间更新、软键盘遮挡、滚动和真实断网恢复。桌面移动 viewport 或 DOM composition 事件不能代替这项结果。

## 整理离线证据

`scripts/verify_multidot_host.py` 仅核验操作者提交的清单结构、候选身份和文件摘要，不连接宿主、不检查日志内容真伪、不授予执行权限。即使通过，仍返回 `host_independently_verified=false` 和 `grants_authority=false`。

准确命令：

```sh
python scripts/verify_multidot_host.py /private/host-manifest.json \
  --evidence-root /private/host-evidence \
  --expected-commit REVIEWED_CANDIDATE_COMMIT \
  --expected-source-sha256 REVIEWED_SOURCE_ZIP_SHA256
```

expected 值来自独立审核的交付渠道。source_sha256 在同一验收内统一使用正式完整源码 ZIP 的 SHA-256，不混用代码变更集指纹、镜像摘要或某个文件摘要。候选尚未提交时不能拿基线提交冒充最终候选提交。

清单严格包含以下顶层字段，多余或缺失字段均不接受：

| 字段 | 准确结构 |
| --- | --- |
| schema_version | 整数 1 |
| source_commit / source_sha256 | 40 位提交 / 64 位小写十六进制摘要，与两个 expected 值一致 |
| versions | 仅 hub、agent、host 三个非空字符串 |
| evidence | 1–64 项，每项仅 id、path、sha256；path 为 evidence-root 下相对普通文件，不允许符号链接或路径穿越 |
| participants | 2–16 项，每项仅 id、identity_mode、evidence_ref；identity_mode 为 independent_grant 或 delivery_ticket；ID 唯一，evidence_ref 引用 evidence.id |
| scenarios | 恰好五项，ID 见下表 |

每项 scenario 仅包含 `id`、`consumer_kind`、`status`、`source_commit`、`source_sha256`、`participants`、`started_at`、`finished_at`、`checks`。真实已完成样本使用 consumer_kind=real_dot、status=passed；participants 引用参与者 ID，team 至少两名。起止时间带时区且结束不早于开始。

checks 的键必须与下表完全一致；每个值仅为 `{"observed": true, "evidence_refs": ["已存在的证据ID"]}`。未执行项目保持未验收，不伪填 true。把额外负面回执放入对应场景的 evidence_refs，并在人工作业记录中逐项核对；当前离线工具没有为每个负面场景设置单独强制键。

| 场景 | checks 精确键 |
| --- | --- |
| duplex | native_subscription、callback_verified、consumer_call、original_thread_reply |
| authorized_work | owner_scope_matched、operation_receipt、original_operation_reused、original_thread_result |
| team | two_distinct_consumers、same_goal、handoff_receipt、artifact_version_verified、aggregate_publication |
| recovery | interruption_observed、original_operation_reused、no_duplicate_effect、no_duplicate_publication |
| subscription | native_refresh_observed、expiry_stops_delivery、revocation_blocks_admission、original_inbox_restored |

manifest 上限 256KiB；每个证据文件须非空且不超过 2MiB，总计不超过 16MiB。ID 由小写字母开头，后接小写字母、数字、下划线或连字符，总长不超过 64。清单可引用多项脱敏回执；摘要一致并不证明其内容是真实观察。工具通过后还须人工审核来源、精确身份、前后因果与上述负面回执。

公开材料只保留必要状态、时间、匿名关系和摘要。原始 callback 请求正文、wake_ticket、consumer_proof、签名材料、私有配置、数据库和原对话保留在私有受控位置，不随公开源码或报告发布。

## 常见阻塞的处理

| 状态 | 处理 |
| --- | --- |
| 工具目录没有新动作 | 先核对运行镜像/源码是否包含新模块，再刷新实际宿主目录；刷新不会升级服务或创建订阅 |
| CONVERSATION_ARCHIVED / 位置过期 | 回到当前房主与宿主流程建立有效接入，不复制旧授权、加入码或订阅 |
| prepared 未激活 | 核对实际宿主 challenge、精确 filters 与回调身份；配置成功不代表原生订阅成功 |
| DOT_CONSUMER_BUSY | 恢复原活跃回合，或按服务返回的恢复条件等待；不要生成更多 run/key 抢占 |
| DOT_WAKE_TICKET_EXPIRED | 未处理输入仍保留；核对订阅健康，走明确原绑定恢复流程，不重新发用户业务输入 |
| DOT_CONSUMER_SUBSCRIPTION_INACTIVE | 恢复原生订阅；不能降级到旧写入口或静默更换 callback |
| DOT_CONSUMER_SCOPE_CHANGED | 停止使用旧 proof；核对当前批准范围并按当前绑定恢复，不沿用旧上下文扩大执行 |
| DOT_INBOX_CHANGED | 重读当前 revision、协调新增输入，再按当前条件推进 |
| operation 为 pending/unknown | 查询原 operation 与恢复回执；状态未确定前不换键重做副作用 |

## 面板同步与移动输入

面板的 SSE 只携带变更提示，随后按当前权限读取正式状态，不执行模型工作或发放消费凭证。页面用单飞读取和一次尾随刷新合并重复提示；页面在后台时停止提示流，回到前台或网络恢复后重新连接并读取。SSE 不可用、能力未开启或连接不健康时仍保留有界轮询兼容，不能只凭连接图标判断业务完成。

更新尽量保留当前输入节点、中文组合输入、焦点、光标和文本选择；需要延后的局部刷新在组合输入或选择结束后协调。历史目标按当前项目和权限范围维护续页进度，后台首屏刷新不应把已展开历史的游标退回；点击加载时遇到刷新竞态，只按最新范围有界重取一次，连续变化显示可重试状态，不能无限追页或混入旧范围结果。

尚未发送的草稿保留在当前页面内存，不承诺页面关闭、进程被系统回收或浏览器刷新后仍可恢复。可见权限或会话改变时按当前范围清理缓存。显式开启的“本机待发保存”是另一项功能，有独立容量、期限和范围检查，不等同于所有草稿自动持久化。真实手机还需核对系统输入法、键盘遮挡、滚动和后台存活行为。

## 隔离持续压力验证

```sh
PYTHONPATH=. python scripts/soak_multidot.py \
  --output .work/multidot-soak-acceptance
```

output 必须是全新的私有 fixture 目录，不能指定现有 Hub 目录。脚本建立独立测试状态和回环 HTTP 接收器，由确定性合成消费者通过公共动作处理输入；不连接真实 Dot 模型，不执行用户真实工作。默认测量 900 秒、3 个 owner 每个 4 个 Dot，共 12 个模拟 Dot；每 5 秒一批输入，并包含慢回调及周期性 503 故障。实际时长还包括建立与末尾排空。

需要改变负载时使用脚本实际支持的 `--duration-seconds`、`--owners`、`--dots-per-owner` 等参数，保留实际值与场景边界。输出 summary.json 的 checks、持久输入与回复关系、排空状态及异常计数一起核对；非零退出或任一检查失败均不能报告通过。健康、慢端点和故障端点的分位数分别解释，fixture 回复时延不是实际模型响应时间，单次隔离压力结果不是生产 SLO 或长期无泄漏证明。

## 回滚与无 Git 部署包

部署和更新流程见 [PANEL_UPDATE.md](PANEL_UPDATE.md) 与 [RELEASING.md](RELEASING.md)。更新提交前、尚未接受新写入时可按原更新事务恢复。新版本已经接收写入后，禁止恢复旧数据库快照覆盖当前 ledger；先评估前向修复或经验证的兼容回退。

回滚准备工具冻结旧版本能识别的准入事实与本次 v2 活动原生订阅；它不停止服务、不授予生产权限，也不自动恢复被冻结的接入。所有 writer 停止后，先对当前数据库生成只读计划，审核精确计划和范围，再应用。运行旧二进制之前核对回执；不要在回退期间使用旧版 rekey，旧代码不认识新增密文列及 sealed body。

Git checkout 可直接核验来源。无 Git 部署包应先在精确审核的 Git 候选导出来源清单：

```sh
# 仅导出源码证明，不访问数据库；输出路径必须尚不存在。
python scripts/prepare_multidot_rollback.py \
  --export-source-proof /private/source-proof.json

# SOURCE_PROOF_SHA 来自独立审核渠道；计划生成默认不修改数据库。
python scripts/prepare_multidot_rollback.py \
  --database /private/hub.sqlite3 \
  --source-proof /private/source-proof.json \
  --expected-source-proof-sha256 SOURCE_PROOF_SHA \
  --plan-out /private/rollback-plan.json

# 所有 writer 已实际停止、精确计划审核后才执行；确认参数不会替你停进程。
python scripts/prepare_multidot_rollback.py \
  --database /private/hub.sqlite3 \
  --source-proof /private/source-proof.json \
  --expected-source-proof-sha256 SOURCE_PROOF_SHA \
  --apply /private/rollback-plan.json \
  --expected-plan-sha256 REVIEWED_PLAN_SHA \
  --confirm-hub-stopped \
  --manifest-out /private/rollback-receipt.json
```

无 Git 模式在 plan 和 apply 两阶段都须提供来源清单及外部预期摘要。工具核对固定源文件、脚本本身和精确版本信息；清单只能证明与给定来源一致，不能代替签名、发布方身份或部署授权。计划过时、范围变化、清单缺失或摘要不符时停止，重新评估实际原因，不能通过删记录、改状态或取消检查绕过。
