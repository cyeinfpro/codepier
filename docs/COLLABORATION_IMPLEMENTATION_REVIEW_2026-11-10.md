# 协作中心三份设计的落地审查报告（2026-11-10）

> 审查对象：`main@7ae8fb7`（`1.33.9`，本地领先 `origin/main` 56 个提交，未推送）。
> 对照文档：
> [统一对话与持续工作](COLLABORATION_CHAT_WORK_DECOUPLING_2026-11-10.md)（下称文档一）、
> [独立宿主连接](COLLABORATION_HOST_CONNECTIONS_2026-11-10.md)（文档二）、
> [可靠投递、证据诊断与发布运维](COLLABORATION_DELIVERY_OPERATIONS_2026-11-10.md)（文档三）。
> 设计基线 `1abc59f` → 当前 HEAD：112 个文件，+10129 / −1077。

## 1. 结论

**代码层面基本落地，但不能说“都做到位了”。** 三份设计里的表结构、入口、事务边界和主要判定都已经写进代码，相关 21 个测试文件 311 项全部通过。差距集中在四处：

1. **有一个会在升级后自动触发的行为回退**（§3 的 F1，已复现）：历史排序回填会把“原本可发”的无关新回复变成被挡，需要房主手工核对旧回复才能解开。建议放量前修。
2. **文档一的核心体验默认是关着的**。`CODEPIER_ROOM_CHAT_SCOPE_MODE` 默认 `strict`，“撤 A 不打断 B”、保留待办让出、过期路线读取都只在 `decoupled` 回合生效；同时这些能力却被无条件声明给宿主（F2）。
3. **真实宿主与真实微信验收没有任何证据**。三份文档各自要求的真实联调（文档一 C6／§15.4，文档二 §10.4／H21，文档三 R5／§14.2）在仓库里找不到记录；`RELEASE.json` 仍是 `prepared / published=false / deployed=false`。
4. **文档与发布说明没跟上**。三份设计文档本身未纳入 git，正文仍写“本轮交付是设计、未实现”，文档一 §16 清单全未勾；`CHANGELOG` 1.33.9 只提了独立宿主连接。

## 2. 审查方法与边界

- 通读三份文档，按章节提取要求；逐条到 `hub/ shared/ scripts/ deploy/ tests/` 定位实现。
- **完整阅读**：`weixin_delivery.py`、`weixin_delivery_order.py`、`weixin_delivery_transport.py`、`weixin_delivery_routes.py`、`room_host_connections.py`、`room_host_proofs.py`、`room_host_rebinds.py`、`room_host_rollout.py`、`room_turn_policy.py`、`consumer_engine.py`、`delivery_compatibility.py`、`shared/hub_data_contract.py`、`telemetry.py`、`room_consumers.py` 前 1120 行、`room_runtime.py` 的 reply 路径。
- **只看了差异或关键函数**：`rooms.py`（`member_scope`／`event_authorize`／`stop_member_work`）、`room_subscription_renewals.py`、`room_host_migration.py`、`room_connections.py`、`weixin_schema.py`、`lifecycle.py`。
- **没有细读**：`room_subscription_routes.py`、`events.py`、`room_diagnostics.py`、`channel_router.py`、`room_weixin_native.py` 的改动，以及 `web/collaboration-room.js`（+949 行）。这些部分下文只标“存在”，不对正确性下结论。
- **实际运行**：
  - `pytest` 21 个相关测试文件：311 passed，0 failed（131 秒）。全量 CI 没跑。
  - 两个一次性复现脚本（放在仓库外，已删除）：验证 F1，并取了候选依赖 SQL 的 `EXPLAIN QUERY PLAN`。
- 没做浏览器验证，没连真实宿主或微信。

下文每条问题都标注了依据类型：**已复现** 或 **代码阅读**。

## 3. 问题清单

### P1 · 建议放量前处理

#### F1 历史回填把可发的新回复变成被挡（已复现）

- **位置**：`hub/collaboration/weixin_delivery_order.py` 的 `_historical_order`（三个返回 `unresolved` 的分支）与 `UNRESOLVED` / `RELATION` 片段；由 `weixin_delivery.py::tick` 每秒调用 `backfill`。
- **现象**：一条 v1 旧投递，有非空组键（如 `work:old`）、有一个 `unknown` 分项、来源已无法核实。回填前它只挡同组后续。回填把它的 `ordering_evidence` 写成 `{"unresolved": true, …}` 后，`UNRESOLVED` 条件命中，关系变成 `legacy_relationship_unknown`，于是**同联系人之后所有回复都被挡**。
- **复现结果**：对同一联系人一条无关的新问题回复，`dependency_state` 回填前 `ready`，回填后 `waiting`；`blocking_predecessors` 返回 `('legacy_peer_unknown', 'legacy_relationship_unknown')`；`candidates()` 变空。
- **为什么是问题**：`unknown` 按设计永不自动结算，所以这个屏障会一直在，直到房主对旧回复点“我已核对收到”。这与文档三 §7.2 第 6 条“无法解析时保持原保守关系”、§4.3“version 1 的旧 work/thread 组在有证据迁移前保持原约束”以及 §10.3“历史未知范围不会扩散到其他工作”不一致：回填是在**扩大**而不是保持原约束，而且是升级后后台自动发生的。
- **建议**：
  1. 对原本有非空组键的 v1 行，回填失败时只记 `ordering_checked_version=2` 和原因，不写会被 `UNRESOLVED` 命中的 `unresolved` 标记；整联系人屏障只留给 `ordering_key=''` 的行。
  2. 上线前先用只读方式统计“未结算且会被判 unresolved”的存量条数，确认影响面。
  3. 补一条回归：回填前后，无关新回复的 `dependency_state` 不变。

#### F2 能力声明是静态的，但实现只对 `decoupled` 回合开放（代码阅读，默认值已用脚本确认）

- **位置**：`shared/room_contracts.py:46-52`（`TURN_ACTION_CAPABILITIES`）、`rooms.py:1354`、`shared/public_collaboration.py:470`；对照 `room_consumers.py::_yield`、`read_scope`、`room_turn_policy.py::_default`。
- **现象**：`consumer_continue_later`、`expired_route_session_read` 对所有房间无条件声明。但 `yield_mode=continue_later` 在非 `decoupled` 回合直接返回 `409 ROOM_CONSUMER_POLICY_REQUIRED`；过期路线带回合凭据的读取也只认 `decoupled`。而默认策略是 `strict`（`.env.example:55`）。
- **影响**：按服务端自己下发的处理说明（“协议声明 consumer_continue_later 时，可用 yield_mode=continue_later…”）行事的宿主，在默认部署下会稳定拿到 409。文档一 §14.1 的前提是“宿主只发送已声明字段”，声明就该等于可用。
- **建议**：按回合策略投影能力（`begin` 回执里已有 `turn_policy`，可以顺带返回本回合可用能力），或者 `strict` 回合对 `continue_later` 降级成 `if_drained` 的结果而不是报错。

### P2 · 应修，不阻塞小范围试用

#### F3 续期成功的健康路线，回合仍按“领取时的原到期 + 900 秒”封顶（代码阅读）

- **位置**：`room_consumers.py::_route_finish_limit`，被 `_check_scope` 与 `_renew_until` 使用。
- **现象**：上限取 `min(当前订阅到期, 领取时到期) + 900`，不看路线是否已被真实续订。文档一 §7.2 写的是“原路线**已自然到期时**的 `expires_at + grace`”。
- **影响**：命中条件是 `decoupled` 回合，或任何已绑定独立宿主的成员（`_independent_turn_route` 对有 `host_connection_id` 的成员恒为真，而独立宿主在新装实例上默认开启）。在订阅临近到期前领取的回合，即使宿主随后正常续订，心跳和写入也会在原到期 + 15 分钟后收到 `DOT_CONSUMER_LEASE_EXPIRED`，低于 3600 秒的 `hard_deadline`。
- **建议**：仅当原订阅当前确实已到期或非 `active` 时才套用收尾上限。

#### F4 回填和发送串在同一个 tick 里，没有故障隔离（代码阅读）

- **位置**：`weixin_delivery.py::tick`：`reconcile → order.backfill → candidates`；`lifecycle.py::loop` 捕获异常后只记 `degraded`。
- **现象**：`backfill` 的整批在一个事务里，`_historical_order` 没有兜底。任何一行抛出非预期异常，事务回滚、游标不前进，下一秒重试同一行；而发送在它后面，**整个 Hub 的微信外发都停**。
- **说明**：正常数据下我没找到能触发的路径，这是防御性缺口，不是已发生的故障。文档二对宿主回填明确要求“进度不被第一条坏数据长期卡住”，排序回填没有同等保护。
- **建议**：`backfill` 调用包一层隔离；单行异常记为 unresolved 并推进游标。

#### F5 shadow 观测只落日志，进不了遥测（代码阅读）

- **位置**：`room_turn_policy.py::record_shadow`（`logging` 的 `extra`）、`room_host_migration.py::record_shadow`（`LOG.info`）。
- **现象**：两处都校验了固定枚举，这点符合设计。但它们没接 `CollaborationTelemetry`，`/api/system/collaboration-telemetry` 看不到；回合策略那条用的是 `extra` 字段，默认日志格式不会输出，实际只剩一行 `room_turn_shadow`。
- **影响**：文档一 §14.3、文档二 §11.2 把 shadow 差异分类当作放量依据（“任何跨所有者/空间/grant 的意外放行都是阻断性缺陷”）。现在这个依据在运维面拿不到。
- **建议**：给 `telemetry.py` 加 shadow 的 Action/Reason 枚举并计数。

#### F6 显式普通回复漏填 `input_handling` 时默认 `pending`，会反复唤醒（代码阅读，设计风险）

- **位置**：`room_runtime.py::save_reply`（`handling is None and explicit and complete and not publication → 'pending'`）；`room_consumers.py::recover`。
- **现象**：`publish_work=false, complete=true` 且没填 `input_handling` 时，来源输入不确认。`yield_after` 因此返回 `pending_input`，回合不交还；回合结束后恢复器按 30／120／600 秒、之后每 1800 秒再唤醒，24 小时内最多 51 次，每次宿主都会再看到同一条消息。
- **说明**：这符合文档一 I11（complete 不能吞掉未准入的执行要求），不是实现错误。风险在于宿主漏填一个可选字段的代价是重复回答，且回执里看不出服务端实际采用了 `pending`。
- **建议**：回执返回实际生效的处置；对“同一来源已有 complete 回复、且之后无新输入”的唤醒限频或合并。

### P3 · 记录在案

| 编号 | 位置 | 说明 | 依据 |
| --- | --- | --- | --- |
| F7 | `weixin_delivery_order.py` 的 `FAILED`；`weixin_delivery_routes.py::authorize_parts` | 重登录隔离会把未发分项置为 `cancelled`，而 `requires_success` 把任何 `cancelled` 都算前置失败。若先恢复后继、再恢复前置，后继会被永久记为 `dependency_failed`。界面的“可恢复”提示不考虑跨投递依赖。 | 代码阅读 |
| F8 | `weixin_delivery.py::queue_reply_result` | `CODEPIER_WEIXIN_DELIVERY_ORDER_V2=false` 时组键回到 v1，但仍写入 v2 `ordering_evidence`（可能含 `unresolved`）并抬高兼容下限；判定 SQL 不看 `ordering_schema_version`。Runbook 说的“按旧组键保守排序”不完全成立。 | 代码阅读 |
| F9 | `weixin_delivery_transport.py::admit`；`candidates` 的 `fair` 排序 | 从未被服务过的联系人序号为 0，永远排最前。持续有新联系人进入时，老积压联系人的等待没有 `ceil(N/16)` 的上界。64 联系人测试覆盖的是固定集合。 | 代码阅读 |
| F10 | `room_host_migration.py::classify / _adopt` | 历史上通过来源链共用一个 dot 的多个房间，回填后各自成为独立 host。符合“不猜测合并”，但存量上“撤销这个 dot 的全部连接”只影响一个房间，连接列表会出现多个同名项。界面和 Runbook 没有说明。 | 代码阅读 |
| F11 | `weixin_delivery.py::reconcile` | `preparing` 租约过期回到 `queued` 时不累加 `preparation_failures`，进程在准备阶段反复崩溃时没有预算上限。 | 代码阅读 |

候选依赖查询的执行计划已核对：`WAITING_PREDECESSOR` 走 `MULTI-INDEX OR`（`weixin_delivery_order` 索引 + 主键），分项子查询走 `delivery_id` 索引，没有全表扫描。大数据量下的实际耗时没测。

## 4. 落地对照

状态含义：**已落地** = 读过实现且有测试；**已落地（未细核）** = 入口和表存在、测试通过，但我没逐行读；**部分** = 见备注；**未做** = 仓库里没有。

### 4.1 文档一：统一对话与持续工作

| 章节 | 要求 | 状态 | 备注 |
| --- | --- | --- | --- |
| §7.1 | 原 begin 恢复先于路线检查 | 已落地 | `consumer_engine.begin` 先匹配原请求；`_begin_scope` 不再要求有效订阅 |
| §5.2 | `turn_policy` / `policy_revision` / `scope_digest_at_begin` / `observed_scope_digest` / `hard_deadline` | 已落地 | 领取时固定；`scope_updated` 不再改写原摘要 |
| §5.3 | 权限变化不作废无关聊天回合，通知带定位 | 已落地，**默认不生效** | 仅 `decoupled` 回合；`strict` 仍整体过期 |
| §6 | 按来源／话题／对象的依赖检查 | 部分 | 回复走 `guard_reply_revision` 的定向判定；工作入口在 `decoupled` 下跳过全局版本闸。没有文档建议的集中 `guard_dependencies` 登记表，各工作入口的对象依赖是否覆盖“同话题新输入应冲突”我没有逐个核实 |
| §7.2 | 自然到期的有界收尾 | 已落地 | 见 F3 |
| §7.3 | 宽限读必须带同一已领取回合的凭据 | 已落地，**默认不生效** | `read_scope`；仅 `decoupled` |
| §8 | 回执先查、`historical_result` / `current_consumer` / `effects_created`、墓碑语义 | 已落地 | `room_runtime.reply`；有并发、回包丢失、墓碑测试 |
| §9.1 | 回复、处置、outbox、回执、origin、让出同事务 | 已落地 | 有“回执写入失败全部回滚”的测试 |
| §9.2 | `publish_work` 省略／false／true 三态 | 已落地 | 合约保留字段是否提供；三态进指纹 |
| §9.3 | `yield_after` | 已落地 | |
| §9.4 | `yield_mode=continue_later` | 已落地，**默认不可用** | 见 F2 |
| §9.5 | 工作通知的持久移交证据 | 已落地 | `room_work_input_handling`，按 attempt 与 fencing token 关联原操作 |
| §10 | 游标不含范围摘要、固定扫描上界、可见性重扫、有界排空 | 已落地 | 游标 v2 + `room_consumer_drain_checks` |
| §11 | 成果与投影的精确可见性 | 已落地（未细核） | `reply_artifacts` 读过；`publication()` 后半段没读完 |
| §14.1 | 能力声明 | 部分 | 见 F2 |
| §14.3 | 开关、按房间覆盖、审计 | 已落地 | 实例管理员 + `expected_version` + 幂等 + 审计；shadow 观测见 F5 |
| §15.4 / C6 | 真实 dot／微信体验验收 | **未做** | 无记录 |

### 4.2 文档二：独立宿主连接

| 章节 | 要求 | 状态 | 备注 |
| --- | --- | --- | --- |
| §6 | 五张表、成员／候选补列、唯一索引 | 已落地 | 与草案一致，另加了候选与路线游标列 |
| §5 | `control_handle`、单次复用挑战 | 已落地 | HMAC 摘要 + 恒定时间比较；挑战绑定来源订阅 |
| §7.1 | 资源动作、HTTP 路由、统一目录 | 已落地（未细核） | 路由和合约注册都在；契约一致性测试没逐项看 |
| §7.2 | 身份闸、撤销闸、首次接入引导 | 已落地 | `member_scope` 在 `off / shadow / on` 三种模式下对 v2 成员都检查撤销 |
| §7.3–7.5 | 新建／复用提交点、路线轮换、批量换绑 | 已落地（未细核） | `register_verified`、批量换绑读过；`room_subscription_routes.py` 没细读 |
| §8 | 影响预览、权威撤销事务、持久清理 | 已落地 | 回执先于版本检查；清理按页持久推进，不取消其他 host 的候选 |
| §9 | 不再凭回调挑战自行延长 TTL | 已落地 | `room_subscription_renewals` 改为等待宿主真实续订 |
| §10.2 | 删除无票据领取 | 已落地 | 代码里已无相关符号 |
| §10.3 | 宿主回合容量 | 未做（设计允许） | `turn_capacity: 'unverified'`；文档要求有证据才启用 |
| §10.4 | 原生续接、插话、并行、主动回传的真实证据 | **未做** | 无记录 |
| §11 | 回填、`auto/shadow/on`、启动器版本门 | 已落地 | Dockerfile、compose、systemd、k3s 入口都加了预检；旧二进制由 schema `11` 哨兵拒绝（已核对基线 `store.py` 会拒绝未知 schema）。shadow 观测见 F5 |
| §12 | H01–H23 | 部分 | 见 §5 |

### 4.3 文档三：可靠投递与发布运维

| 章节 | 要求 | 状态 | 备注 |
| --- | --- | --- | --- |
| §3 | 事务边界、`EnqueueResult`、回执内 `channel_dispositions` 及表达式索引 | 已落地 | `queue_reply_result` 要求已有事务 |
| §3.3 | 迟到回执作为不可变观测 | 已落地 | `weixin_dispatch_attempts` + `weixin_late_observations`，按原发送身份核对后采用 |
| §4.1–4.4 | v2 回复链、类型化依赖、候选与诊断共用判定、claim／发送前复核 | 已落地 | 见 F1、F7 |
| §4.5 | 重登录不抹去原未知结果 | 已落地 | 新世代不迁移旧队列；未发分项需房主逐项批准恢复 |
| §5 | 重试分类、预算、`Retry-After`、分项级确认与停止预览 | 已落地 | `control_plan` + `preview_digest` |
| §6.1 | 持久联系人轮转 | 已落地 | 见 F9 |
| §7 | 可恢复回填 | 已落地 | 见 F1、F4 |
| §7.3 / §9 / §11.3 | 阻塞关系、未领取分类、有界诊断 | 已落地（未细核） | 分类名和上限都在；权限过滤没逐项核 |
| §8.2 | `room_reply_origins` | 已落地 | 与唯一 DDL 一致 |
| §10 | 类型化遥测与遗漏计数 | 已落地 | 进程内计数，重启清零 |
| §10.2 / §10.3 | 两次调用的真实样本、p50/p95/p99 | **未做** | 无记录 |
| §11 | 界面 | 已落地（未验证） | 前端有改动，我没做浏览器验证 |
| §12–13 | 上线顺序、回退、兼容下限 | 部分 | Runbook 和兼容下限已有；实际只到“代码合入本地 main”，未发布未部署 |
| §14.2 | 真实宿主与微信场景 | **未做** | 无记录 |

## 5. 测试覆盖

相关测试全部通过，但密度明显低于三份文档的验收矩阵（文档三 D01–D27，文档二 H01–H23，文档一 §15.2 的 16 条竞争用例）。按测试名和内容对照，下列场景**没找到对应测试**（按名称匹配，可能有遗漏）：

- 文档三：D08 回填与 claim／取消／新写入并发；D19 受限项目／跨账号诊断不越权；D20 大数据定点查询与索引命中；D26 旧进展在新回答之后迟到；F1 对应的“回填不改变无关回复可发性”。
- 文档二：H05 两个并发复用请求进同一房间；H06 探针延迟期间 host 被撤销；H07 撤销清理中途重启；H14 宿主续订与维护响应并发；H17 回合容量（功能未做）；H20 秘密不出现在列表／日志／诊断的输出检查。
- 文档一：§15.2-7 心跳／过期／新 begin／yield 竞争；§15.2-10 巨大不可见前缀与多页空结果；§15.2-12 `continue_later` 后立即重启；§15.2-16 新旧客户端组合。

另外 F2、F3 两处都没有测试会失败，说明 `strict` 默认值下的能力声明、以及续期后的回合寿命没有被断言过。

## 6. 文档与发布状态

- **三份设计文档未纳入版本控制**（`git status` 显示为未跟踪），而实现已在 main 上。文档头部仍写“本轮交付是设计……新增字段、迁移、接口和代码均为待实施设计”，文档一 §16 的 12 项完成清单全部未勾。建议入库，并在头部加一行实施状态指向本报告。
- **`CHANGELOG.md` 1.33.9 只写了独立宿主连接**。同一版本里还有：回复协议的 `publish_work` / `yield_after` / `input_handling` / `continue_later`、回合策略开关、微信投递 v2 排序与分项级控制、协作兼容下限（schema `11`，旧二进制不可回退）、遥测端点。其中兼容下限会影响回退，应当写明。
- **Runbook**（`COLLABORATION_DELIVERY_OPERATIONS_RUNBOOK.md`）自述“记录真实证据前，阶段状态为待核验”，与本报告结论一致。

## 7. 建议的收口顺序

1. 修 F1 并补回归；上线前统计存量 unresolved 影响面。
2. 修 F2（能力按策略投影）和 F3（收尾上限只在路线确实到期时生效），各补一条测试。
3. F4、F5 一起做：回填隔离 + shadow 计数进遥测。没有 F5，`shadow → decoupled` 的放量没有依据。
4. 决定 `CODEPIER_ROOM_CHAT_SCOPE_MODE` 的放量路径。当前默认 `strict` 意味着文档一的主要体验没有对任何房间开启。
5. 按 Runbook 的证据表做一轮真实宿主 + 测试微信联调，至少覆盖文档三 §14.2 的十个场景。
6. 三份文档入库并更新状态；补 CHANGELOG。

## 8. 本报告没有覆盖的内容

- `events.py`、`room_subscription_routes.py`、`room_diagnostics.py`、`channel_router.py`、`room_weixin_native.py` 的改动和整个前端，只确认了存在和测试通过。
- 文档一 §12 的入口清单没有逐个入口核对身份／依赖／恢复三列。
- 没跑全量测试、浏览器测试和任何性能测试。
- 本次只新增了这一个文件，没有改动代码，也没有提交。
