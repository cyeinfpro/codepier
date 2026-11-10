# 协作中心：独立宿主连接与持续接入（2026-11-10）

> 实施设计 v2.1。本文负责连接身份、跨房间复用、订阅路线、恢复与撤销；与[统一对话与持续工作](COLLABORATION_CHAT_WORK_DECOUPLING_2026-11-10.md)、[可靠投递、证据诊断与发布运维](COLLABORATION_DELIVERY_OPERATIONS_2026-11-10.md)共同构成一套设计。本轮补足第 10.4 节真实 dot 体验的能力前提。
>
> 源码核对基线：`main@1abc59f905ca670f6d51133bacce6a0b0a1676ce`（`1.33.8`）。本次逐段核对了 `room_connections.py`、`room_pending_bindings.py`、`room_subscription_renewals.py`、相关 `rooms.py` / `room_consumers.py` / `events.py` 入口，以及现有连接测试。
>
> **实施状态**　1.33.9 已在本地实现独立宿主身份、迁移、路线轮换和撤销。下文的现状分析以设计基线为准。基线回归和缺口见[实施审查](COLLABORATION_IMPLEMENTATION_REVIEW_2026-11-10.md)，真实宿主、微信及部署组合的证据按[运维手册](COLLABORATION_DELIVERY_OPERATIONS_RUNBOOK.md)登记。真实往返和宿主容量仍待验证。
>
> 公共协议核验日期：2026-11-10。代码片段是关键契约与实现骨架，不是可不经适配整段粘贴的完整补丁。合成回归和真实验收分别按实际结果记录。

## 1. 本轮结论与必须修正的地方

独立连接实体是正确方向，但 v1 仍把「回调地址」「宿主身份」「已受理通知」「持续授权」部分混在一起。按原稿直接实施，会出现错误合并两个 dot、撤销后自动复活、回退绕过撤销，以及界面显示已保活而宿主已停止处理等问题。

| v1 的决定或缺口 | v2 的确定设计 | 对用户的实际改善 |
| --- | --- | --- |
| 用 `(space, owner, grant, endpoint_digest)` 唯一识别宿主 | 使用随机、不透明且不可复用的 `host_connection_id`；地址只是路线属性，显式复用须有原连接控制证明 | 同地址的不同 dot 不会混成一个；同一 dot 换地址也不必丢掉其他房间 |
| `verified()` 找不到 active 记录就自动新建 | 新建必须关联首次接入或明确换绑的原操作；已撤销实体及其绑定认领记录保留 | 后台回填和迟到探针不能把停掉的连接重新打开 |
| 开关关闭后不再检查宿主实体 | 撤销闸独立于产品灰度开关，所有路径都检查 | 回退功能不会恢复已经撤销的访问 |
| 对全部成员同步清理前 256 条，其余按订阅状态补扫 | 权威撤销原子提交，独立持久清理任务分批收尾；清理限定原成员、原世代、原订阅 | 大量房间也能立即停止后续准入；新换绑的连接不会被旧清理任务误伤 |
| 续期挑战成功就自行延长订阅 TTL | 区分持续接入意愿与协议订阅寿命；有限 TTL 由真实 `events/subscribe` 更新 | 页面状态与宿主实际接入一致，短暂故障自动恢复而不反复索要授权 |
| 通知被受理后，允许无票据抢领 | 标准路径保留票据；只在未来存在独立、可验证的原生宿主证明时扩展领取方式 | 解决兼容问题时不把同 grant 的另一个 dot 当成原消费者 |
| 所有探测更新一个 `version` | 分离 `authority_version` 与 `health_version` | 心跳和续期不会让房主刚打开的操作失效 |
| 列表统一显示 `ready` | 分开显示已登记连接、当前路线健康、房间可执行能力 | 不再把「历史验证过」说成「现在能收到、能工作」 |

本轮不增加第二套任务引擎，不把全部房间搬到一个订阅，也不要求用户每天处理凭据和恢复向导。连接解决「通知到哪一个已登记的消费者」，项目授权与操作执行仍由现有房间、Goal、Work 和原操作回执约束。

## 2. 产品体验先确定

用户连接一个 dot 后，应在同一对话中讨论、交办、追问和修改；加入另一个房间改变其席位、权限与可见上下文，不要求创建“聊天 dot”与“工作 dot”两个人格。连接验证通过只证明控制关系与往返可用。原生对话上下文、长期记忆、模型并行和持续运行由宿主能力决定，不因创建本地 ID 自动成立；其体验门槛见第 10.4 节。

### 2.1 新接入与复用

首次接入只展示三个事实：给连接起一个名字、选择在哪些项目里做什么、当前验证到哪一步。项目和能力来自现有房间授权，不能因接入 dot 自动扩展。一次接入流程内，邀请码、原生订阅、探针与持久化回执自动接续；刷新页面继续原流程，不重新创建成员。

「使用已有 dot」列表展示名称、使用中的房间数、最近一次实际往返、当前恢复状态。选中后创建目标房间自己的成员和订阅。系统自动完成原连接控制证明及目标往返验证；需要宿主执行的原生订阅动作由已授权的接入流程发起，不要求用户复制一串内部标识。宿主确实没有相关能力时，保留原流程并显示唯一可行的下一步。

连接列表不是项目授权列表。复用 A 房间的 dot 到 B 房间，不复制 A 的项目、消息、凭据、工作、消费位置或接入批准；B 的权限沿用 B 已保存的设置。房间与微信产生的输入使用同一成员绑定及授权判定。

### 2.2 局部动作与全局动作

| 用户动作 | 影响对象 | 界面说明与后续行为 |
| --- | --- | --- |
| 暂停此房间 / 暂停此成员 | 当前房间或成员 | 其他房间照常运行；消息继续按已授权入站规则保存 |
| 从此房间移除 dot | 当前成员席位 | 不从全局连接列表抹掉仍被其他房间使用的连接 |
| 给此成员更换 dot | 当前席位的绑定 | 新连接验证成功才切换；失败时保留可用的原连接 |
| 恢复连接 | 当前故障路线 | 在原授权内恢复订阅和补投，保留消息、工作与回执标识 |
| 撤销这个 dot 的全部连接 | 当前工作区、当前所有者下选定的 HostConnection | 展示确切受影响房间后一次完成；不顺带撤销整个 OAuth grant |
| 重新登录 / 批量更换连接 | 用户本次选中的成员集合 | 一次选择后后台逐项验证、切换；成功、等待、失败分项展示 |

「暂停此房间」与「撤销全部连接」必须有不同入口和不同文案。普通恢复无确认弹窗；全局撤销在原连接详情内展示影响清单和一个明确按钮。仅当影响对象发生实质变化时刷新差异，不因健康心跳、消息数量或工作进度变化要求再次确认。

### 2.3 接入状态与操作文案

| 显示状态 | 必须存在的证据 | 系统自动做什么 | 用户主操作 |
| --- | --- | --- | --- |
| 等待接入 | 持久接入流程与有效目标 | 等待真实宿主加入，流程到期前给准确提示 | 继续接入 |
| 正在验证 | 原生订阅成立，探针尚未往返 | 在有界重试预算内恢复探针 | 查看详情 |
| 已连接 | 当前绑定有效且有真实往返 | 按宿主协议维持路线、按原消费位置处理 | 发消息 |
| 正在恢复 | 仅网络、限流或续订等暂时故障 | 后台退避，保留输入，不重复创建任务 | 默认无操作 |
| 等待宿主续订 | 本端有限 TTL 已到，未收到真实续订 | 停止该订阅投递；继续保存可接受的入站消息 | 在原宿主继续连接 |
| 授权已到期 | 当前 grant 或批准期限已到 | 保留待办及结果，停止越界准入 | 更新授权 |
| 已暂停 | 当前房间或成员的显式暂停记录 | 保留队列，不触发其他房间暂停 | 恢复 |
| 已撤销 | 持久宿主撤销记录 | 拒绝新动作，收尾已派发操作与订阅 | 更换连接 |

连接详情采用页内展开；移动端单列展示状态、影响范围和一项主操作。工程标识、探针和错误栈放在可展开的诊断区。白底、黑色正文、黄色仅用于重点操作与正在处理状态；不能用大面积警告色表示正常等待。实时状态更新保持滚动与焦点，不重建整页或让弹窗反复出现。

## 3. 当前实现与证据

以下是源码现状，不是新能力已经落地的声明。

| 位置 | 已核对的事实 | v2 的改动点 |
| --- | --- | --- |
| `room_connections.py::lineage` | 逐跳读取来源成员，检查非叶子房间就绪、grant 与保存的绑定世代，最多 32 跳 | 把运行时依赖改为独立连接；保留链仅用于迁移证明与旧协议适配 |
| `room_connections.py::attach_source` / `allows_shared_route` | 复用必须是房主显式选择；目标有独立成员与范围；共享 grant 本身不足以复用 | 保留显式关系和隔离语义，不用相同 URL 替代它 |
| `rooms.py::member_scope` | 每次刷新 principal，检查成员所有者、grant、世代、证明与当前房间状态，再调用连接检查 | 在公共身份闸加入宿主撤销检查；`stopping`/历史回执路径分别限定用途 |
| `rooms.py::event_subscription_guard` | 当前成员地址改变会报 `ROOM_HOST_CHANGED`；重复路线要有显式复用关系 | 加入受控路线轮换，候选验证后再切换 |
| `room_pending_bindings.py::prepare` / `activate` | 新绑定放在候选表，成功时核对 `base_generation`，再停止旧策略并切换 | 扩展候选的目标宿主与撤销快照，不破坏已存在的安全切换结构 |
| `rooms.py::event_subscription_attached` | 订阅标识改变或验证重置可重置探针；普通同订阅续期已有恢复路径 | 区分 TTL 刷新、签名轮换、路线换绑和成员换绑 |
| `room_subscription_renewals.py::finish` | 回调挑战成功后本端写长 `expires_at`，目前没有宿主实际续订回执这一前提 | 按第 9 节改为协议一致的维持方式 |
| `events.py::dispatch_candidates` | 同一 `endpoint_key` 的在途投递互斥，按所有者与订阅有持久公平扫描位置 | 不把传输互斥误认为宿主模型回合限流 |
| `rooms.py::stop_member_work` | 停止原策略并保留原操作标识，结束活动消费回合；是否撤销旧桥接由参数控制 | 全局撤销必须按原绑定精确调用，不能清掉新候选或重跑原操作 |
| `tests/test_room_connections.py` | 已有独立证明、共享路线、跨房间权限/消息/水位隔离、来源暂停与换绑的合成测试 | 改写原有耦合预期，增加本设计的竞争、撤销和迁移用例 |
| `web/collaboration-room.js` | 现有 `keep_connected` 与 `set_renewal` 入口承载「由 CodePier 定期续期」语义 | 改为「自动保持连接」及真实续订方式/等待原因，不能只改文案而保留协议偏差 |

## 4. 三份文档共同遵守的契约

### 4.1 标识和版本各管一件事

| 字段 | 唯一责任 | 不能承担的责任 |
| --- | --- | --- |
| `host_connection_id` | 指定已登记的连接实体，公开引用 | 不是秘密，不是项目授权，不是可直接接入的令牌 |
| `control_handle` | 证明对该连接的控制，仅用于复用、路线轮换或原流程恢复 | 不授予读项目、开工、发消息、查询其他成员凭据的能力 |
| `host_revocation_epoch` | 单调递增的连接撤销闸；会话和待执行动作捕获它 | 不随心跳或续订变化，不由灰度开关关闭 |
| `authority_version` | 连接控制与绑定成员集合的实质变更 | 不因探针、健康观测、消息计数变化 |
| `health_version` | 路线健康投影与可恢复故障变化 | 不用于授权 CAS 或全局撤销预览的确认条件 |
| `binding_generation` | 单房间成员换绑的世代 | 不因普通 TTL 续订而增加 |
| `route_generation` | 当前成员订阅路线或签名身份的实质变化 | 不代表宿主获得了新项目权限 |
| `scope_revision` | 当前房间成员项目能力变化 | 不作为宿主实体的身份，也不作废其他房间聊天 |
| `session_id` / `fence` | 当前消费回合及并发写入资格 | 不代表通知已送达，不等价于工作执行权 |

每个回合保存其 `host_connection_id`、`host_revocation_epoch`、成员世代和文档一的 `turn_policy` / `policy_revision`。活动回合可以按领取时的策略完成允许的动作，但明确撤销、成员移除和当前对象授权始终实时生效。历史回执读取允许按文档一返回经过当前可见性投影的摘要，不能顺便恢复执行。

### 4.2 不变量

1. 同一 grant 下可以有多个完全独立的连接；同一回调地址也可以是多租户网关。只有地址相等，不能合并连接。
2. 同一连接可以因宿主路由策略拥有不同的回调地址。只要存在经验证的控制连续性，不因地址不同拆成两个身份。
3. 每个房间成员拥有自己的邀请、证明、订阅、探测、授权、水位和消费回合。宿主实体不承载这些房间数据。
4. 暂停 A、移除 A、归档 A、给 A 换绑，不改变 B 的宿主状态。来源成员不是宿主实体的运行时父对象。
5. 宿主撤销是终态。重新接入创建新的实体或新的明确候选；旧实体、旧 handle、旧票据不会复活。
6. 新连接验证成功前不修改旧绑定；切换必须校验当前成员世代、目标宿主状态和原操作幂等记录。
7. 网络成功、回调挑战成功、`2xx`、往返探针成功各有独立含义；都不自动增加项目权限。
8. 元数据和显示名称只用于展示、相关性分析；`openai/session`、user agent、URL 摘要都不是授权凭据。
9. 已派发的外部操作保留原操作标识与实际结果。取消或撤销不宣称已经回滚其副作用。

## 5. 身份与控制证明

### 5.1 默认身份方案

新连接在首次接入流程完成真实往返验证后登记随机 `host_connection_id`。验证之前走第 7.2 节严格限定的接入引导路径，只能建立目标订阅和交换探针，不能处理业务。创建关联原接入操作、成员、绑定世代、订阅及往返证据。它代表 CodePier 验证并登记的一条控制关系，不宣称能从公开元数据识别一个物理设备、一个模型实例或一个永久不变的 dot。

不建立 `(grant, endpoint_digest)` 唯一索引。地址摘要可用于诊断和传输限流；签名密钥仍按原订阅加密保存，不集中复制到宿主表。不同订阅使用不同签名密钥不会创建新的宿主身份。

首次成功交换可返回受限 `control_handle`，由宿主连接适配层保存；模型日常业务调用继续使用现有成员身份，不重复搬运这个 handle。该能力是新增设计，不能把当前工具中任意通用 handle 假定成已经具备本节约束。

| 项目 | 约束 |
| --- | --- |
| 生成 | 至少 256 bit 的加密随机秘密；数据库存查找标识与带服务器密钥的摘要，比较使用恒定时间 |
| 绑定 | 所有者、工作区、grant、host ID、host 撤销世代、明确用途与有效期 |
| 使用 | 只允许绑定/路线控制接口，仍需目标房间的房主授权操作；不能单凭 handle 新建任意房间成员 |
| 恢复 | 只在同一接入操作的原回执中，核对原请求指纹及原成员证明/现存控制凭据后受限重取；同 grant 加操作 ID 不足以取回秘密；不得放进普通列表、诊断、通知正文、公共链接或日志 |
| 轮换 | 原能力可证明连续性时先生成候选并验证，再原子替换；旧能力仅在有界交换恢复窗口内返回原回执，不接受新目标 |
| 撤销 | 每次使用实时核对 host 状态与 epoch；旧 handle 无法清除撤销记录 |
| 丢失 | 优先通过已有真实验证路线完成一次性挑战；均不可用时继续原接入流程，不从 URL、grant 或会话元数据推定同一身份 |

如果宿主没有安全持久保存 handle 的能力，使用下面的一次性复用挑战，不要求模型自行把长期密钥记进文字笔记。原 handle 的恢复材料只允许加密保存在原交换回执的受限字段；普通历史回执投影不返回它。所有证明均丢失时回到真实挑战/接入流程，不能靠原操作 ID 降级恢复秘密。

### 5.2 复用挑战

房主选择「把此连接加入目标房间」后，服务端生成短时、单次、仅针对该目标的挑战。挑战绑定：原 host 及 epoch、目标成员与目标候选世代、房主原操作、到期时间。通过原连接中可用的、独立验证过的原生路线交给宿主；目标成员仍单独建立其真实原生订阅与往返探针。

接受挑战须同时核对当前所有者/grant、挑战秘密、host 状态、目标房主操作和目标世代。使用一次后保存原兑换回执；同键重试读回执，不能兑换到另一个目标。挑战受理与目标预选关联在同一事务提交。源成员只是这次挑战的传输证据，兑换成功后不成为目标运行时依赖。

可用路线优先选最近实际往返成功的当前绑定；某房间暂停时不向该房间发送普通业务输入，可使用同 host 的其他允许路线。拥有有效 control_handle 的宿主也可主动继续已存在的目标接入流程。没有可用证明时，目标显示「等待原 dot 接入」，不冒充已经复用成功。

共享网关能否把指定订阅的挑战投给原消费者，必须由真实往返证明。仅收到 HTTP `2xx` 不足以登记或复用。两个 dot 即使名称、grant、URL 甚至网关相同，只要没有原连接控制证明，就保持两个连接。

## 6. 数据模型与约束

优先扩展现有持久化记录，不另建宿主级任务或邮箱。下面列出所需新增信息；DDL 是迁移草案，现有数据库补列应使用项目原有的幂等补列方式。

```sql
CREATE TABLE IF NOT EXISTS room_host_connections (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'revoked')),
    host_revocation_epoch INTEGER NOT NULL DEFAULT 0
        CHECK (host_revocation_epoch >= 0),
    authority_version INTEGER NOT NULL DEFAULT 1,
    health_version INTEGER NOT NULL DEFAULT 1,
    established_by_operation_id TEXT NOT NULL,
    established_by_member_id TEXT NOT NULL,
    established_by_generation INTEGER NOT NULL,
    verified_at REAL NOT NULL,
    revoked_at REAL,
    revoked_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS room_host_establishment
    ON room_host_connections(established_by_operation_id);
CREATE INDEX IF NOT EXISTS room_host_owner
    ON room_host_connections(space_id, owner_user_id, state, created_at, id);

CREATE TABLE IF NOT EXISTS room_host_binding_adoptions (
    member_id TEXT NOT NULL,
    binding_generation INTEGER NOT NULL,
    host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
    evidence_kind TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (member_id, binding_generation)
);

CREATE TABLE IF NOT EXISTS room_host_control_handles (
    id TEXT PRIMARY KEY,
    host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
    host_revocation_epoch INTEGER NOT NULL,
    secret_digest TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('binding_control')),
    state TEXT NOT NULL CHECK (state IN ('active', 'retiring', 'revoked')),
    expires_at REAL NOT NULL,
    created_by_operation_id TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS room_host_attach_challenges (
    id TEXT PRIMARY KEY,
    host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
    host_revocation_epoch INTEGER NOT NULL,
    target_member_id TEXT NOT NULL,
    target_generation INTEGER NOT NULL,
    owner_operation_id TEXT NOT NULL,
    secret_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'consumed', 'expired', 'cancelled')),
    expires_at REAL NOT NULL,
    consumed_by_operation_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE (owner_operation_id, target_member_id, target_generation)
);

CREATE TABLE IF NOT EXISTS room_host_revoke_jobs (
    host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
    host_revocation_epoch INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done')),
    after_member_id TEXT NOT NULL DEFAULT '',
    processed_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (host_connection_id, host_revocation_epoch)
);
```

`room_members` 增加 `host_connection_id TEXT NOT NULL DEFAULT ''`、`route_generation INTEGER NOT NULL DEFAULT 0`、`connection_identity_version INTEGER NOT NULL DEFAULT 1`。版本 1 只表示迁移前已存在且尚未转换的绑定；新接入从创建起写 2，旧绑定在完成认领的同一事务升到 2，不能由调用方选择或降级。`room_pending_bindings` 增加候选的 `host_connection_id`、`host_revocation_epoch`、`route_generation`、控制证明来源和证明完成时间。`PendingBindings.facade` 必须投影候选值，不能继承原成员的 host ID；未确定候选 host 时为空，仅进入接入引导路径，不把原 host 身份带进候选验证。

补列完成后增加以下索引。目标成员在 `invited` 阶段即占据复用名额，避免两个并发接入各自通过应用层查重。历史重复先在迁移报告中处理，不能靠删除成员使建索引通过。

```sql
CREATE UNIQUE INDEX IF NOT EXISTS room_member_live_host
    ON room_members(conversation_id, host_connection_id)
    WHERE host_connection_id != '' AND status != 'removed';
CREATE INDEX IF NOT EXISTS room_member_host_lookup
    ON room_members(host_connection_id, id);
```

绑定认领表是防复活的关键：同一 `(member_id, binding_generation)` 一旦登记，始终映射到原实体，包括该实体已撤销的情况。后台回填不得绕过这张表，再创建一个新的 active host。首次接入和明确换绑由新的原操作及新的绑定世代建立新记录。

`verified_at` 记录真实验证时间，不能被普通列表读取或健康探测不断刷新。`established_by_*` 是历史证据，不用于在每次业务调用中查询另一个成员是否还活着。

## 7. 入口、事务与关键实现

### 7.1 对外操作保持紧凑

沿用 CodePier 的 `query / act` 入口风格。当前 `shared/unified_contracts.py` 已有 `collab.list`、`collab.get`、`collab.connect`、`collab.room` 分组；`room_member_create` 已存在于 `shared/room_contracts.py::ROOM_MUTATION_ACTIONS`。当前连接列表仅见 `room_api.py` 的 `GET /connections` 调用 `RoomConnections.listing`，没有可直接调用的 `room_connection_list` 资源动作。下表明确区分新增与扩展，不能把拟议动作写成现有能力。

| 资源动作与注册状态 | `query / act` 映射 | 关键输入 → 输出 |
| --- | --- | --- |
| `room_connection_list`，新增 | `query(action='collab.list', view='room_connection_list')` | `cursor, limit` → 安全连接列表、下一页游标 |
| `room_connection_view`，新增 | `query(action='collab.get', view='room_connection_view')` | `host_connection_id` → 健康、使用房间数、可行下一步，无秘密 |
| `room_member_create`，扩展现有 | `act(action='collab.room', view='room_member_create')` | 现有房间/成员参数 + 可选 `host_connection_id`（与 `reuse_member_id` 互斥）→ 原成员、接入流程与回执 |
| `room_connection_control`，新增 | `act(action='collab.connect', view='room_connection_control')` | 原目标操作引用、受限控制证明、幂等键 → 已存在接入/路线轮换流程的当前进度 |
| `room_connection_impact`，新增 | `query(action='collab.get', view='room_connection_impact')` | `host_connection_id, cursor, limit` → 完整集合摘要、分页成员、`authority_version` |
| `room_connection_revoke`，新增 | `act(action='collab.connect', view='room_connection_revoke')` | host ID、`impact_digest, expected_authority_version`、幂等键 → 原撤销回执、受影响数量、清理状态 |
| `room_connection_rebind_batch`，新增父操作 | `act(action='collab.connect', view='room_connection_rebind_batch')` | 所选成员及各自预期世代、新连接目标、幂等键 → `batch_id` 与原候选子项 |
| `room_connection_rebind_status`，新增只读投影 | `query(action='collab.get', view='room_connection_rebind_status')` | `batch_id, cursor, limit` → 每项候选/切换状态、原回执引用 |

新增查询登记到 `ROOM_QUERY_ACTIONS`，新增变更登记到 `ROOM_MUTATION_ACTIONS`；`rooms.invoke` 明确委派给 `RoomConnections`，并更新 `shared/public_collaboration.py` 的公开映射和 `shared/unified_contracts.py` 的 action/view 注册。统一入口最终解析到相应 `collaboration_room_connection_*` 服务动作。批量换绑子项复用现有 `room_connect_resume` 的 `rebind` 行为及 `PendingBindings`，不再复制另一套切换实现。

连接清单、影响预览与全局撤销保持当前房主控制边界：面板会话及 CSRF 校验不变，服务层继续核对所有者/工作区；把动作放进统一目录不等于授予普通 MCP grant 房主管理权。若当前调用主体没有该控制权，`help` 和能力投影应明确显示不可用入口，而不是在适配器中伪造 panel principal。成员可用的 `room_connection_control` 只接受第 5 节规定的受限目标操作与控制证明。

HTTP 层沿用 `room_api.py` 的既有 router 前缀：保留 `GET /connections`，新增 `GET /connections/{id}`、`GET /connections/{id}/impact`、`POST /connections/{id}/revoke`，其他控制/批量入口按相同资源动作定义绑定。HTTP、manifest、MCP schema、`help` 和接入说明从同一操作/合约注册表派生，并用契约测试比较可见动作、字段和响应；禁止各自维护不一致的枚举或越过同一个 service 方法。

拟议只读请求与精简响应示例（`host_example` 仅为文档占位，不是真实标识）：

```json
{
  "request": {
    "action": "collab.get",
    "view": "room_connection_view",
    "project": "MCP",
    "args": {"host_connection_id": "host_example"}
  },
  "response": {
    "host_connection_id": "host_example",
    "state": "active",
    "display_state": "recovering",
    "authority_version": 3,
    "health_version": 8,
    "room_count": 2,
    "next_action": "wait_for_host_refresh"
  }
}
```

公开返回中的 `id` 不可突然从 member ID 改成 host ID。新增响应明确使用 `host_connection_id`，旧适配器继续返回确切可用的 `reuse_member_id` 与 `reuse_binding_generation`。不存在可用旧锚点时标记该旧能力不可用，不能把已移除成员或一个看似相近的成员交给旧客户端。

服务端返回能力字段说明当前可调用的操作。例如 `connection_capabilities` 包含 `host_reference`、`verified_reuse`、`route_rotation`、`host_revoke`；宿主不识别的新请求字段不得在旧合约上试探发送。能力发现说明可用操作，不授予执行权。

### 7.2 身份闸与撤销闸

下面是已激活独立连接的检查骨架。它由已经完成 principal / 成员证明核对的入口在同一业务事务内调用，不能单独作为完整授权函数。尚未转换的 `connection_identity_version=1` 由明确的兼容适配器执行完整旧校验；新接入引导由本节后面的专用入口处理，两者都不是通用空 host 放行。

```python
def require_host_current(store, principal, room, member, *, expected_epoch=None):
    host_id = member.get('host_connection_id')
    if not host_id:
        raise DevError('ROOM_CONNECTION_INVALID', '已激活成员缺少连接身份', 409)
    host = store.one('SELECT * FROM room_host_connections WHERE id=?', (host_id,))
    if (not host or host['space_id'] != room['space_id']
            or host['owner_user_id'] != room['owner_user_id']
            or host['owner_user_id'] != principal.user_id
            or host['grant_id'] != principal.grant_id
            or host['grant_id'] != member['grant_id']):
        raise DevError('ROOM_CONNECTION_IDENTITY_REQUIRED', '连接身份需要重新核对', 403)
    if host['state'] != 'active':
        raise DevError('ROOM_CONNECTION_REVOKED', '这个 dot 的连接已撤销', 409)
    if expected_epoch is not None and expected_epoch != host['host_revocation_epoch']:
        raise DevError('ROOM_CONNECTION_REVOKED', '原连接已失效', 409)
    return host
```

必须覆盖：普通成员读写、`consumer_begin`、活动回合写入、开工/续办准入、候选激活、订阅创建/续订、事件预留与实际派发、托管维持、控制 handle 使用。检查当前 host 与 epoch 后，还要继续执行各入口原有的当前对象/项目授权。

不存在 host 的已激活新协议成员是完整性错误，不能落回旧路径。兼容适配器必须检查服务端保存的身份契约版本；转换完成后删除旧适配分支。只读历史回执、原操作状态归档和清理另设明确用途，不能通过 `stopping=True` 给通用读写开后门。

**首次接入不能形成循环依赖。** 现有接入流程／`PendingBindings` 中保存短期 bootstrap 上下文：房主原批准操作、目标成员及候选世代、已验证的调用者 grant、原邀请／候选证明、到期时间及单次探针状态。该上下文只由专用 `require_bootstrap_flow` 验证，返回与业务身份不同的受限类型，不能传给普通 `begin/inbox/reply/work`。

| 引导动作 | 允许范围 | 必须拒绝 |
| --- | --- | --- |
| 注册／刷新目标候选订阅 | 仅原接入操作与目标候选世代对应的订阅，经过真实回调验证 | 换成其他房间、目标、grant，或发普通业务事件 |
| 投递接入探针、读取／回复探针 | 专用目的票据与随机挑战；只含接入所需标识，无项目内容；重放只返原探针回执 | 读取业务 inbox、使用普通消息回复入口、创建工作 |
| 完成首次验证／激活候选 | 同事务重查原批准、候选 CAS、探针与当前身份；先创建／认领 host，再绑定并通过完整 host 闸 | 绕过已撤销认领、复用已撤销的预选 host、接受过期或另一候选的证据 |

预选已有 host 的 bootstrap 仍实时检查该 host 的撤销状态；完全新 host 尚无 ID 时，仅验证它对应的首次批准流程。候选已取消、房主撤回接入、授权到期或目标世代变化，立即停止该引导路径。bootstrap 不能接收由客户端任意指定的 `purpose` 来扩大能力；原探针到齐后由服务端自动完成激活，无需用户再确认一轮。

### 7.3 新建与复用的提交点

1. 邀请创建、目标成员占位、房主预选 host、原操作幂等记录同一事务提交。连接撤销与新增目标并发时，数据库唯一约束和当前 host 检查决定唯一结果。
2. 回调验证与网络探测在事务外执行。保存开始时的成员世代、候选版本、host epoch 和订阅身份；返回后在事务中重查，再写证据。
3. 往返成功后先查原操作及绑定认领记录。已经认领则返回原 host；已撤销则停止。不能因为找不到 active host 而自动新建。
4. 首次独立接入生成随机 host；明确复用的目标必须落到预选 host。相同 endpoint 既不触发合并，也不成为禁止复用的理由。
5. 目标订阅与探测均成功后，在同一激活事务内先创建／认领 host、写入成员或候选 host 引用、校验完整 host 闸，再提交绑定与版本切换；bootstrap 随之关闭。保留目标房间原授权，源成员状态此后不再参加目标业务校验。

### 7.4 路线轮换与成员换绑分开

| 变化 | 验证 | 版本与已有回合 |
| --- | --- | --- |
| 同订阅普通 TTL 刷新 | 当前身份、当前授权、原生续订请求 | 不改绑定世代；仅更新真实到期与健康信息 |
| 同路线签名密钥轮换 | 经过认证的续订和当前连接关系；新旧签名按协议短窗交接 | 增 `route_generation`；已领取回合不因签名轮换失效 |
| 同连接回调地址变化 | 原连接控制证明 + 候选路线挑战 + 真实往返 | 先保留旧路线，验证后原子切换路线世代；不改变其他房间 |
| 给成员选择另一个 dot | 房主原换绑操作 + 新连接独立验证 | 增 `binding_generation`；结束旧消费回合，保留原工作与操作回执 |
| grant 换成新授权实体 | 原房主认证与新连接验证 | 建立明确的新连接及候选，按所选成员逐项切换；不得仅因账号相同静默迁移 |

同 URL 的密钥轮换不要求用户重新批准项目权限。URL 变化只有在证明控制连续性并满足回调地址校验后自动继续；没有连续性证据则保持候选，不宣称是同一 dot。地址校验沿用既有防止内网目的地址和跳转的发送器，不能因「原连接已信任」跳过新地址校验。

票据绑定成员世代、原订阅和 route_generation。正常密钥轮换中已经发出的有效票据可在明确的短交接窗口内兑换；原 route 被明确撤销、host 被撤销或成员换绑则立即停止新兑换。旧票据对应的已领取回合遵循文档一的固定回合策略和当前身份闸，不受纯 TTL 健康抖动牵连。

### 7.5 候选失败与批量接入

候选超时、验证失败、用户取消或 CAS 不一致，只结束该候选。保留原成员、原宿主与原订阅；不要先清空 `host_connection_id` 再试接新连接。

批量接入是一个父操作下的多个独立候选。一次用户选择后无需逐房间再确认。父操作重试时读取原子项状态；已切换项不重新换绑，未完成项继续原候选。某房间当前权限收窄只影响该项；其他项继续。全部完成后给结果摘要，不以一项失败回滚已经成功且可用的连接。

## 8. 全局撤销：立即生效，安全收尾

### 8.1 影响预览

影响摘要只含会改变授权意义的数据：host ID、epoch、当前引用它的成员 ID 与绑定世代、成员所属房间。完整有序集合在服务端计算摘要；列表可以分页，但不能只用第一页生成摘要。心跳、探针时间、消息计数、工作进度、健康版本不进入摘要。

预览展示当前房间数、排队中的工作数、已经派发的操作数，以及「已派发操作可能仍在结束，将继续核对原结果」。详情可展开，不把项目路径和秘密暴露到全局列表。

### 8.2 权威事务

以下是事务边界骨架。`mutation_receipts`、`revoke_impact`、`record_host_change` 为须按现有持久化能力实现的建议适配接口，不是仓库已存在的函数。

```python
def revoke_host(service, principal, args):
    with service.store.transaction():
        owner, host = service.owned_host(principal, args['host_connection_id'])
        # 原回执先于动态影响/版本检查；仍核对调用方与请求指纹。
        previous = service.mutation_receipts.find(owner, 'host_revoke', args)
        if previous is not None:
            return service.project_revoke_receipt(owner, previous)
        impact = service.revoke_impact(host)
        if (host['authority_version'] != args['expected_authority_version']
                or impact['digest'] != args['impact_digest']):
            raise DevError('ROOM_CONNECTION_IMPACT_CHANGED', '受影响的连接已变化', 409)
        changed = service.store.execute('''UPDATE room_host_connections
            SET state='revoked', host_revocation_epoch=host_revocation_epoch+1,
                authority_version=authority_version+1, revoked_at=?,
                revoked_reason='owner_revoked', updated_at=?
            WHERE id=? AND state='active' AND authority_version=?''',
            (service.clock(), service.clock(), host['id'], args['expected_authority_version']))
        if changed.rowcount != 1:
            raise DevError('STALE_VERSION', '连接状态已变化', 409)
        current = service.read_host(host['id'])
        receipt = service.make_revoke_receipt(current, impact, args)
        service.enqueue_revoke_cleanup(current, receipt)
        service.record_host_change(current, receipt)
        service.mutation_receipts.save(owner, 'host_revoke', args, receipt)
        return receipt
```

撤销闸、清理任务、变化通知与原回执一起提交。事务中不发网络请求，也不循环执行所有房间的复杂停止逻辑。响应丢失时同键取得原撤销回执，不因版本已增加误报失败。

### 8.3 后台收尾必须有持久进度

清理任务逐页读取仍引用该 host 的原成员，并在每一项操作前核对 `(member_id, binding_generation, host_connection_id, subscription_id)`。随后停止这个绑定的后续策略准入、作废旧消费回合、停止原续期、关闭原订阅、结束尚未派发的通知。每页进度同事务保存；崩溃后从持久游标继续，不靠进程内 `_after`，也不以「订阅已经 unsubscribe」代表所有清理完成。

只取消指向被撤销 host 的候选；如果一个成员已经在验证新的独立 host，候选继续存在。旧清理任务不能调用无条件 `pending.cancel(member_id)`，也不能对已经切换的新世代执行 `stop_member_work`。

新动作每次检查撤销闸，因此立即失效不依赖清理速度。外部操作和网络发送的边界按文档三处理：尚未派发的拒绝；已经进入派发阶段的标记在途并核对原回执，不宣称网络请求可以被追回。结果归档由受限的原操作通道完成，不重新开放撤销消费者的执行入口。

### 8.4 与 grant 撤销及历史读取的关系

撤销 host 不撤销整个 grant。相同 grant 下其他 host、普通项目工具不会被联带关闭。反向的 grant 撤销/到期仍约束所有使用该 grant 的 host；列表可以展示「授权已失效」的历史连接，不把它伪装成可复用状态，也不把它隐藏得无从恢复。

房主仍能查看原消息、原工作和原操作证据。被撤销的 host 不能借历史查询重新获得已经失去的正文或附件；只读回执按文档一的身份证明与当前投影规则处理。全局撤销按钮不得删除记录、改写原结果或重置幂等键。

## 9. 持续接入与订阅续期

### 9.1 保留每成员一个原生订阅

每房间成员的订阅继续绑定 `(owner, conversation, member, binding_generation)`。它保留独立过滤条件、水位、签名、到期与恢复；宿主实体只把同一消费者的控制关系连起来。一个房间到期或暂停不导致其他房间失去通知。

一宿主一订阅虽然减少续订次数，却会改变事件路由、游标、批处理及故障边界，当前没有收益证据支持这次重构。本设计不做。通知可以合并为「邮箱已变更」，原始消息和工作不能被合并丢失。

### 9.2 协议依据与 v1 更正

OpenAI 的 [MCP Events 官方文档](https://developers.openai.com/plugins/build/mcp-events)（2026-11-10 核验）区分真实续订、回调挑战与异步受理；有限订阅到期需停止投递，刷新由 `events/subscribe` 返回新期限。仅在宿主请求且服务端授予无到期订阅时，才可返回空期限。签名轮换与回调安全校验仍适用。

因此删除 v1「挑战通过后自行延长 TTL，即使与协议不完全一致也保持不变」的决定。回调还活着只能证明该地址能回应挑战，不能证明宿主仍保留这个订阅或继续承担原自动任务。

### 9.3 三种时间不能混为一个

| 时间/状态 | 谁决定 | 到期后的行为 |
| --- | --- | --- |
| 用户希望持续接入的期限 | 房主已保存的连接设置 | 允许在范围内自动恢复连接；不自行授予项目权限 |
| grant 与房间能力有效期 | 现有授权系统 | 停止新越界动作；待办及原回执保留 |
| 当前原生订阅 `refreshBefore` | 实际订阅交换的结果 | 停止该订阅的新投递；等待真实续订，不抹掉消息 |
| 已领取回合的 lease / 固定策略 | 文档一的回合机制 | 在当前身份仍有效的前提下完成允许的回复/交还；不靠延长订阅伪造有效期 |

`keep_connected` 表示后台持续维护意愿，不再解释为「服务端可单方面续订」。正常同身份续订和瞬断恢复沿用已保存授权，自动完成，不弹第二次项目授权确认。

| 宿主实际能力 | 实施方式 |
| --- | --- |
| 支持标准有限 TTL 自动续订 | 接受实际 `events/subscribe` 刷新，保留身份匹配的订阅和消费位置；监测快到期状态 |
| 原生请求无到期订阅且服务端允许 | 显式按协议授予；仍实时检查 grant、房间暂停和撤销，不把无到期当无限权限 |
| 原生适配器提供可验证的续订 API | 在既有持续接入授权内调用该 API，保留实际请求与响应记录，再更新本端状态 |
| 宿主仅响应 challenge，没有续订能力 | 不能代写到期时间；在原到期前显示恢复入口，到期后准确显示等待宿主续订 |
| 宿主不支持原生订阅 | 保留待接入和输入；明确当前能力缺口，不创建假 callback 或宣称后台自动工作 |

维护器每次取得工作后再次核对当前 host epoch、成员世代、授权期限和路线；网络响应返回后再用原版本 CAS 提交。某次宿主续订先完成，旧维护任务不得覆盖新到期时间、签名或路线。

### 9.4 自动恢复的节奏

采用现有有界退避并增加抖动，防止大量房间同刻续期。优先处理最早到期且仍有待办的订阅，同时保留公平扫描，空闲房间不会永久失去连接。按订阅保存恢复尝试和下次时间；一次宿主故障只形成一个持续故障事件，不向用户连发相同提醒。

对可确定暂时性的网络错误与限流自动恢复；明确 unsubscribe、host revoke、grant revoke、成员移除不能通过恢复器改回 active。用户恢复原暂停房间时，只恢复当初由该房间动作暂停的策略；换绑与授权变化重新按当前对象判断。

密钥轮换不应因 `credential_digest` 不等直接永久停止托管维持。来自实际已认证续订的新密钥，经过当前版本校验后同步到维护绑定；未经认证的变化仍拒绝。轮换窗口内使用原协议支持的双签，窗口结束不保留旧密钥用于新投递。

## 10. 通知缺正文、宿主并发与能力边界

### 10.1 不把「受理未领取」当根因

回调返回 `2xx` 而票据未兑换，可能是宿主排队、暂停、限流、运行失败、批处理合并、票据到期、接口目录过旧，也可能是正文没有交给模型。这个指标只能说明该通知尚无领取证据，不能单凭数值非零证明正文缺失，也不能单凭零证明正文始终完整。

诊断先关联原 event、delivery、ticket、session 和宿主实际运行记录；显示「通知已受理，尚未开始处理」。排查同一次运行的批量票据与原回合，避免把未使用的第二张票据误判成故障。

### 10.2 不实施 v1 的无票据开关

删除 `ticketless_claim`、`accepted_ticket()` 及「房主开启后仅凭受理记录代领票据」路径。它失去「调用者持有这次通知内容」这一证据，让共享 grant 且拿到成员证明的另一个 dot 能抢占原消费者回合；开启一个界面开关不能补回缺失证明。

标准领取继续使用真实 `wake_ticket`。未来只有在宿主提供独立的、可验证的运行回执或绑定控制通道时，才为该适配器新增领取能力：至少绑定 host、member、generation、subscription、event、有效期和单次消费状态，并有明确验证方。新能力需可发现、可回放测试及真实往返证据；不能把普通 `_meta["openai/session"]` 拼成凭据。

OpenAI [插件参考](https://developers.openai.com/plugins/reference)（2026-11-10 核验）将 `openai/session` 描述为同一会话内工具调用的关联标识。这里仅把其脱敏指纹用于诊断，不因缺失或变化阻塞合法恢复，也不据此认定原 dot 身份。

### 10.3 宿主并发是独立的容量控制

当前按 endpoint 串行的是 HTTP 在途投递，不是模型回合。两个房间被依次通知后仍可能同时领取。host ID 适合统计活跃回合和公平调度，但不能先假定所有宿主只支持一个回合，从而把现有吞吐降成串行。

容量默认沿用现有行为并记录能力为未知。经适配器文档或真实验收确定上限时，启用 `max_active_turns`；在原 `begin` 幂等回放之后、创建新会话之前，同事务预留容量。已存在的回合重试不占第二个名额；租约到期、交还、撤销释放名额。调度按房间公平轮转，不能让一个高频房间长期占满。

容量不足返回「已排队」及可恢复引用，不写成任务失败，不唤醒后立即重试成通知风暴。待领取事件可合并同一成员的 dirty 提示，消息与工作原件仍在。过期票据的后续恢复沿用原待办与幂等语义，不能通过重新创建工作绕开排队。

共享网关的传输限流仍按 endpoint，宿主回合限流按经证明的 host；两者不可互换，也不因相同地址让不同 dot 共用执行额度。

### 10.4 真实 dot 的连续体验必须分别验证

`host_connection_id` 是连接控制关系，member 是房间席位，consumer session 是短期领取／提交资格，Goal／work／operation 是工作与原结果。以上对象均不等于宿主原生 conversation，也不创建第二个模型。产品沿用用户选择的 dot 展示身份；有权限的工作记录通过文档一第 4.3 节接续，不因内部回合变化表现为换了一个助理。

| 能力 | 可接受的证据 | 能力缺失或未验证时的行为 |
| --- | --- | --- |
| 原生对话续接 | 宿主实际支持的续接接口／运行记录，配合前后文验收 | 通过当前授权的原工作与消息重建必要背景；不承诺原生私有上下文或完整记忆共享 |
| 运行中插话与调整 | 真实活动运行接到补充、读取当前要求，后续效果按新要求提交 | 保存补充并安排下一次真实处理；服务器对已记录控制变更执行后续效果闸，不宣称立即打断模型 |
| 边工作边回答 | 可独立执行的已派发工作，或宿主有证据的多运行能力 | 允许输入保存并显示真实等待；不以多个 consumer session 或空 queued 记录冒充并行 |
| 跨回合持续推进 | 原 operation／持久 continuation 有有效恢复责任者与真实执行／唤醒机制 | 显示等待继续处理；没有独立执行者时不能仅因发过“开始了”就显示持续运行 |
| 完成后主动回传 | 原结果落库、唤醒、实际领取、读取结果、reply 及渠道处置的整条证据链 | 恢复真实订阅与待办；尚未领取时显示该事实，不能从 2xx 推断 dot 已处理 |
| 同一 dot 的记忆与插件 | 宿主配置和实际调用表明该连接可用的能力 | 不从同 grant／URL／host ID 推断同一记忆，不能把私人聊天或别房间内容自动授权到当前房间 |

适配器能力记录沿已有发现／诊断机制维护，保留证据来源、宿主版本和核验时间；广告能力与实际运行结果分别记录，未知不自动变成支持。没有证明并行也不人为给所有宿主强加单回合限额，仍按第 10.3 节保持现有准入并测量真实行为。

OpenAI 的[官方 dot 使用说明](https://developers.openai.com/training/walkthroughs/personalize-your-dot)描述了向 Work 委派任务、继续沟通和随优先级变化跟进的用法，也说明保存的笔记不等于完整对话历史。这里以连续协作为产品目标，不能据此推导第三方 MCP 已获得原生对话路由或所有记忆接口。官方 [MCP Events](https://developers.openai.com/plugins/build/mcp-events) 只将 2xx 定义为接收确认，后续处理异步进行；具体接到哪次运行以及何时回答仍需实际记录核验。

真实验收须对照直接使用该 dot 的同类过程：先讨论后交办、工作中追问和索取部分成果、修改／停止、关闭页面后主动交付、跨真实宿主运行续接、房间与微信往返、订阅续期和重启恢复。记录是否需要重述、是否重复执行、实际等待时长、用户是否看到模式切换。只有这些场景有证据后，才能声明已达到相应维度的 dot 使用体验；不承诺跨宿主相同的界面、逐字流式显示、时延或模型并行度。

## 11. 迁移与回退

### 11.1 先部署共同撤销基础，再开启新体验

采用 expand → backfill → verify → activate → retire 的迁移。先发布理解 host 撤销闸、绑定认领记录和新记录读取的桥接版本，再生成会被这些语义约束的新数据。数据库迁移保存持久 checkpoint 和批次错误；每批有时间/行数预算，锁等待退避，不把完整房间清理放进启动事务。

版本门分两层，单写数据库 marker 不足以阻止完全不认识它的旧二进制。第一层位于部署控制器/安装器/服务启动器：在启动目标程序并允许它打开业务数据库读写前，按实际发布版本与持久兼容声明校验最低 reader/writer 版本；所有受支持的更新、容器启动和回滚入口共用该检查，未知旧版本拒绝启动。第二层由桥接版及之后的程序自身检查数据库 `min_reader_version` 与数据契约版本。提升能力门前必须先停止旧实例、确认旧 writer 已退出，再允许兼容实例访问；不能让新旧程序同时写同一个数据库后指望旧程序理解新 marker。部署脚本、启动器、镜像入口和数据库版本标记的兼容规则必须一同交付。

回填按成员自身当前 grant、往返证据和明确历史复用关系工作。不能仅按 endpoint 分组。历史指针可证明一组连接的控制连续性时才归入同一个 host；存在循环、缺失源世代、grant 不一致或证据缺口时保留独立记录/待核对状态，不猜测合并。

原来源成员已经换绑时，当前源成员代表的新 dot 不能被当成旧目标的原 host。能证明目标自己原往返的，按独立的历史绑定实体认领；是否与其他旧目标合并，必须有额外的明确复用证据。该处理修复耦合，不转移权限。

只有自身往返证据的旧房间可能出现同名独立连接。面板标出单房间连接的所在房间，撤销按连接实体及其影响清单执行。多个房间可以通过显式复用验证接入同一连接。

所有认领写入 `room_host_binding_adoptions`。重跑使用原认领，包括已撤销结果；不得扫描到 revoked 就另造 active。没有往返证据的成员继续原待验证流程，不能回填为「已连接」。

迁移使目标房间从来源故障中恢复时，只按目标自己的当前房间、成员、授权与原工作状态自动继续。目标本身被暂停、移除、撤权或已取消的操作保持原状；结果未知的操作先核对原回执。

### 11.2 灰度开关的精确定义

| 模式 | 行为 |
| --- | --- |
| `off` | 不准入新的独立连接能力；已存在的宿主撤销闸和最小读取约束仍必须执行 |
| `shadow` | 在不产生新副作用的前提下比较旧来源链与独立实体判断；撤销闸共同生效 |
| `on` | 已完成认领的成员使用独立实体；未转换旧绑定走明确的兼容路径 |

`shadow` 的差异必须分类：来源房间暂停/归档、来源换绑、历史证据缺口、当前撤销、身份不匹配等。预期改善是来源故障不再影响目标；任何跨所有者/空间/grant 的意外放行都是阻断性缺陷。不能把所有「旧拒绝、新允许」统称预期。

观测使用明确的类型化协作遥测接口，固定事件名与字段白名单。当前 `hub/mcp_request_audit.py::mark` / `Trace.record` 不是可接受任意关键字的日志字典；直接写 `mark('room_host_shadow', legacy=..., host=...)` 会在有 trace 时出错或丢弃未知阶段。实施时扩展受测 schema 或使用独立适配函数，观测失败不能改变业务授权或回执提交结果。高基数 ID 留在受控 trace，指标仅用有限原因枚举。

### 11.3 回退不能绕过撤销

| 情况 | 允许的回退 |
| --- | --- |
| 新界面或新能力有问题 | 停新准入，保留已存消息、工作和回执，已有回合按其固定策略结束 |
| 已开启独立连接，需要恢复旧判定 | 使用仍理解撤销闸与认领记录的桥接版本；健康旧路径可降级，撤销仍有效 |
| 存在新字段请求或无旧来源锚点的连接 | 先停止发送不兼容请求，用能力声明选择兼容入口；不能宣称旧二进制天然识别 |
| 旧二进制不认识宿主撤销或新绑定语义 | 部署/启动器在启动它、开放数据库读写前拒绝不兼容版本；先停旧 writer，再选择桥接版回退。数据库 marker 只是兼容声明，不能单独约束不识别它的程序 |

v1「取消订阅后旧二进制就不会复活连接」不成立：清理可能尚未完成，旧调用方还可能重新订阅。保留来源指针有利于兼容读取，但不能替代强制撤销闸。所有活跃实例均达到桥接版本且部署/启动器的版本拦截已验证后，才提升数据库能力门；不允许未知旧写入者混跑。绕过受支持启动器直接运行旧程序不属于可承诺安全的回退方式，部署配置必须防止这种无检查路径取得原数据库写入权。

回退测试必须覆盖未清理完的撤销、刚完成的新候选、正在发送的事件、已领取回合、未归档操作与未知外部结果。保留来源指针直至回退窗口结束；删除旧分支由文档三的可验证收口条件决定，不靠等固定天数自行认定安全。

## 12. 验收矩阵

以下定义验收矩阵。已覆盖的定向场景见[实施审查](COLLABORATION_IMPLEMENTATION_REVIEW_2026-11-10.md)，合成测试和真实宿主验收分开记录。

| 编号 | 场景 | 必须满足的结果 |
| --- | --- | --- |
| H01 | 同一 host 进入 A/B；暂停、移除、归档 A | B 可领取、回复和按其现有权限工作；B 水位与订阅不变 |
| H02 | A 换到新 dot，候选成功与失败分别发生 | 成功仅切 A；失败保留 A 原连接；B 始终指向原 host |
| H03 | 两个不同 dot 使用同 grant、同地址 | 未经控制证明不合并；双方凭据、消息、房间与回执互不可代用 |
| H04 | 同一 dot 在不同房间使用不同回调地址 | 有控制连续性和各自真实探针后可复用；无证明则保持等待 |
| H05 | 两个并发复用请求加入同一房间 | 唯一约束使只保留一个有效成员/接入流程，同键读原回执 |
| H06 | 探针响应延迟期间 host 被撤销或目标换绑 | 迟到响应被 CAS/epoch 拒绝，不创建新 active host |
| H07 | 全局撤销，成员数超过一页，进程中途重启 | 撤销事务后新准入全部拒绝；后台从持久位置继续；无漏清理 |
| H08 | 撤销后成员已切换新 host，旧清理稍后运行 | 新订阅、新候选和新策略不被旧任务取消 |
| H09 | 预览期间只有心跳/消息进度变化 | 原撤销预览仍可用；成员集合实质变化时给精确差异 |
| H10 | 撤销响应丢失，同键重试 | 返回原撤销结果；不再增加 epoch、不重复发通知 |
| H11 | 撤销后 `verified`、回填、旧探针、旧 handle 重试 | 全部保持撤销，不自动复活 |
| H12 | 同订阅普通续期与签名轮换 | 不改变 binding_generation，不结束正在回复的合法回合；旧钥按窗口退出 |
| H13 | 有限 TTL 到期但 challenge 仍成功 | 不发送到期后的应用数据；显示等待真实续订，消息保留 |
| H14 | 宿主真实续订与维护响应并发 | 旧维护响应不覆盖新订阅、签名和期限 |
| H15 | 明确 unsubscribe、grant revoke、房间 pause | 恢复器不能误当瞬断恢复为 active；其他独立 host 不受牵连 |
| H16 | 批量事件、受理未领取、事件正文缺失 | 诊断区分证据，不代填票据；原生不支持时准确报告原因 |
| H17 | 宿主回合容量耗尽及重放 begin | 排队不丢消息；原回合重试不占第二名额；房间间公平 |
| H18 | 迁移有循环、缺失世代、同地址不同连接 | 不猜测合并；错误批次可恢复；进度不被第一条坏数据长期卡住 |
| H19 | `on → shadow/off` 与旧二进制启动 | 撤销始终有效；不兼容读取者被版本门阻止 |
| H20 | 控制 handle / 邀请 / 签名密钥出现在输出检查中 | 普通列表、事件摘要、错误、trace、日志、诊断均不含秘密 |
| H21 | ChatGPT 真实两个房间及微信往返 | 手机收到预期回复，房间记录、宿主运行、原回执逐项对应；不是仅凭HTTP受理判成功 |
| H22 | 原实际外部操作已派发后撤销 | 没有重复执行；展示实际终态/结果未知；不宣称自动撤销了副作用 |
| H23 | 首次无 host 接入、新 dot 候选换绑、引导期尝试业务调用 | 合法订阅与探针能完成并自动激活；引导证明不能领取业务回合、读项目或执行；迟到/已撤回候选不创建 active host |

真实宿主验收至少记录：宿主类型和版本、服务版本、实际原生订阅请求及脱敏响应、每次路线/签名变化、到期与续订时间、事件受理/模型领取/回复原回执。分别测试相同地址与不同地址、批处理开关、跨完整 TTL、宿主重启、服务重启、断网和账号重新连接。

## 13. 实施顺序与完成定义

| 顺序 | 交付内容 | 进入下一步所需证据 |
| --- | --- | --- |
| 1 | 公共撤销闸、版本门、原回执读取及关联字段 | 强制撤销在所有入口生效；兼容版不能绕开 |
| 2 | 独立 host 表、控制证明、目标绑定唯一约束、候选扩展 | H03–H06/H11/H20 通过，当前完整隔离测试保持 |
| 3 | 幂等回填与影子比较 | 全量候选分类，无误合并、无静默复活；有代表性故障注入 |
| 4 | 新接入、复用、局部恢复与全局撤销界面 | H01/H02/H07–H10 的服务与移动交互验收 |
| 5 | 真实协议续订、签名轮换及准确状态投影 | 跨实际 TTL 和宿主版本的验收，不以challenge替代 |
| 6 | 批量重新接入、宿主容量策略（若有上限证据） | 原工作/回执保持、局部失败不拖住全部房间 |
| 7 | 灰度扩展与旧链退役 | 满足文档三的样本、故障率、恢复与兼容门槛 |

完成不是「新表存在」「某接口返回 200」或「等了七天」。必须能解释任意连接的控制证据、当前路线寿命、它影响的房间、撤销后的在途结果，以及每个失败状态的自动恢复或唯一人工操作。真实宿主能力未证实时，文档和界面都保留明确的未验证状态；这不妨碍先实施已经证实可独立交付的隔离、原回执、迁移和准确诊断。

## 14. 来源与证据使用说明

- 项目源码：第 3 节的文件/符号及基线提交；旧代码的行为不等于新设计已经通过验收。
- OpenAI [MCP Events](https://developers.openai.com/plugins/build/mcp-events)：仅用于确认订阅交换、到期、异步受理、签名轮换等协议约束；本文的连接实体、迁移与 UX 是本项目设计判断。
- OpenAI [插件参考](https://developers.openai.com/plugins/reference)：仅用于确认会话元数据的公开用途；不据此推定 dot 的长期身份或授权。
