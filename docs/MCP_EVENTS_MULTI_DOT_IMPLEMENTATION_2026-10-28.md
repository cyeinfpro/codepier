# MCP Events 与多 Dot 协作实施及验收记录

> 历史记录的公开脱敏版；不代表当前部署或授权状态。内部标识、路径和回执已移除。

> 本文保留首轮候选与 5960 项 fast 的历史记录，第 6 节描述首轮当时的未完成项。后续真实回滚/进程恢复、移动浏览器、15 分钟持续压力及新发现问题的修复，见 [扩展验收记录](MCP_EVENTS_MULTI_DOT_EXTENDED_ACCEPTANCE_2026-10-28.md)；历史环境核查方法见 [运行环境就绪审查](MCP_EVENTS_MULTI_DOT_LIVE_READINESS_2026-10-28.md)。不能把首轮次数称作当前候选的全量复跑。

日期：2026-10-28
设计依据：[MCP_EVENTS_MULTI_DOT_DESIGN_2026-10-28.md](MCP_EVENTS_MULTI_DOT_DESIGN_2026-10-28.md)
开发工作区：`[已脱敏标识]`
代码基线：`[已脱敏标识]`
候选代码变更集指纹：`[已脱敏标识]`（base_commit加63条变更/新增path、SHA、mode；排除docs避免报告自引用；不是原工作目录整树hash）

实现、fast及定向回归来自隔离worktree。63个代码文件已按逐文件SHA校验写回原MCP工作区，63个目标文件复核一致，原有8个无关dirty/new文件的SHA保持不变。随后在原MCP目录执行20项smoke并全部通过，分别记录如下。两份文档随最终校验清单交付。

代码写回回执：`[已脱敏标识]`；该次落地manifest摘要：`[已脱敏标识]`。该摘要用于这次63文件落地核对，与页首代码变更集指纹用途不同。

这是候选源码的实施对照和验证记录。基线 commit 不是新增实现的候选 commit；本轮变更尚未提交、推送或部署。最终构建及回归证据绑定上述隔离候选变更集，不能仅以基线hash代替，也不覆盖原工作目录中保留的无关修改。

## 1. 已落实的边界

- 外部仍使用 `collaboration_query`、`collaboration`、`collaboration_work` 三个工具族；扩展动作通过严格判别联合校验，兼容旧私有别名。
- v2 配置先进入 prepared，原生订阅通过 challenge 后才进入 active。读连接、读收件箱和 Rescan 不完成协议升级，也不创建消费权限。
- 一个 Dot 绑定一个稳定的 Inbox 订阅。消息、工作、交接、结果和控制提示引用原业务事实；事件通知不是任务本体。
- 消费者 session/fence/proof 只控制当前 Dot 消费推进权。工作批准、work lease/fence、目标项目和 operation 原回执仍须独立成立。
- 唤醒票据只送往原已验证 callback；普通查询不返回票据或 proof。同 grant 的多个 Dot 是明确标示的同信任域逻辑路由，不声称具备独立聊天身份或凭据隔离。
- 团队批准来自已认证的面板房主。MCP 工具不公开创建团队批准、暂停/恢复批准或修改用户意图的房主动作。
- 团队来源由房主显式选择 team，并且只提及唯一主责。额外多 @ 不自动变成多个完整工作；受控拒绝且事务不留下半条消息、绑定或 Goal。其他成员通过工作指派和有界 handoff 参与同一 Goal。
- 接收、处理、执行、结果、验收与发表分别记录。HTTP 2xx、consumer_begin 成功和页面更新都不代表真实模型或业务任务完成。

## 2. 代码落点

| 范围 | 主要实现 |
| --- | --- |
| 公共契约与集成 | `shared/public_collaboration.py`、`shared/collaboration_contracts.py`、`hub/collaboration/service.py`、`api.py` |
| 投递调度 | `events.py`、`network.py`、`lifecycle.py`：提交后提示、持续补位、全局/端点/owner 配额、持久冷却及公平游标 |
| 统一 Inbox | `dot_mailbox.py`、`shared/dot_mailbox_contracts.py`、`event_contracts.py`、`joining.py`、`dots.py` |
| 消费与逐输入关系 | `dot_consumers.py`、`shared/dot_consumer_contracts.py`、`dot_chat.py`、`message_handling.py`、`dot_relay.py` |
| 团队与原 Goal | `team_collaboration.py`、`shared/team_collaboration_contracts.py`、`coordination.py`、`coordination_schema.py`、`chatroom.py`、`delegation.py` |
| 前端增量更新 | `relay_hints.py`、`web/collaboration-relay.js`、`collaboration.js`、`collaboration-goals.js`、`collaboration-dots.js` |
| 密钥与恢复 | `hub/keyring.py` 轮换新增密文列与带前缀事件正文；`hub/store.py` 检测缺失密钥；`scripts/prepare_multidot_rollback.py` 生成并应用精确冻结清单 |
| 离线证据检查 | `scripts/verify_multidot_host.py`；仅校验证据结构、候选版本和文件摘要，永不声明独立验证宿主或授予权限 |

表中未带目录的后端文件均位于 `hub/collaboration/`，前端文件均位于 `web/`。

## 3. 原设计 28 个问题逐项对照

“已有”表示复用基线能力；“新增”表示本候选实现。测试入口是可重跑的代码，不等于该入口在最终冻结源码上已经通过。最终执行结果另列，真实宿主/设备门禁单独记录。

| # | 问题与实施状态 | 代码和验收入口 | 尚需完成或明确限制 |
| --- | --- | --- | --- |
| 01 | 新增通用 emit 提交后唤醒；回滚不唤醒、重复业务事件不重复唤醒，保留扫描恢复 | `service.emit`、`lifecycle`；`test_multidot_relay_hints.py`、投递回归 | 真实部署上的排队延迟尚需采样；不能由内存回调推导宿主启动延迟 |
| 02 | 新增持续补位，单慢端点不阻止其他 callback 完成；持久 lease/fence 不重复占用 | `events`：全局 4、单端点 1、多个 owner 时 owner 上限 2；`test_events_multidot_dispatch.py` | 端点按owner/Space与完整callback URL区分；不是按物理主机/IP限流。合成慢端点实验与真实网络故障分开报告 |
| 03 | v1 严格保序；v2 新增只合并尚未尝试、尚未封装的提示，保留全部业务输入和单调水位 | `events.coalesce_mailbox`、`dot_mailbox`；burst测试、不可变重试与安全水位回归 | 已尝试或已有持久正文的投递不合并；保留旧事件原ID和原字节以便回放 |
| 04 | 新增 owner/Space 与端点公平、持久次数/字节 token bucket、重启游标、Retry-After≤1800s、稳定抖动、有界重试和冷却；预算不足按实际补充时刻再唤醒 | `events`、`network`、`lifecycle`；25项dispatcher定向回归和同场景前后测量 | 全局8次/s、burst16；owner4次/s、burst8；对应字节预算128/64KiB/s、burst256/128KiB。真实负载应据观测调参，不能为追延迟跳过预算 |
| 05 | 新增每 Dot 稳定 filters/generation；Goal/工作通过 Inbox 引用承载 | `dot_mailbox.subscription`、`team_collaboration`；团队 50 Goal 功能用例、公开双 Dot 工作闭环 | 不会为每 Goal 原生订阅；真实宿主是否自行重复订阅仍需回执核对 |
| 06 | 新增health、到期前每原subscription version最多一次维护提示、原callback受限恢复；native过期恢复保持binding generation并更新wake generation；永久锁定原订阅身份 | `dot_mailbox`、`dots`；即将到期/短TTL/到期恢复及同grant换URL接管拒绝测试 | 没有伪造native renewal；跨到期窗口自动续订仍待真实宿主验证。callback迁移不得冒充续订，需要明确重新绑定流程 |
| 07 | 新增 consumer receipt/session，读接口保持被动；2xx 与实际 begin、逐输入处理分开 | `dot_consumers`、`message_handling`；公开 callback→begin→原消息回复用例 | begin 只证明服务器接到合成消费者调用，不证明真实模型响应、思考或完成执行 |
| 08 | 新增仅 callback 可兑换的票据、单活跃消费者、单调 fence、加密 proof；激活 v2 后旧写口同样校验 | `dot_consumers`、`dot_chat`、`dot_relay`；`test_dot_consumer_sessions.py`、公开旧入口和跨 slot 对抗测试 | proof 不是业务批准；真实宿主仍必须持久保存原发送正文和幂等键 |
| 09 | 新增持久 dirty/drained revision，同事务 yield CAS；读空不等于消费完成 | `dot_mailbox.mark_drained`、`dot_consumers.yield_session`；新输入在 yield 前后提交、旧 fence 回归 | 依赖业务事实正确进入 mailbox；团队 source 完成登记属于本轮跨模块验收重点 |
| 10 | 已有独立 grant；新增同 grant 定向票据和精确 Dot/session 绑定，读接口不返回 bearer 材料 | `dot_consumers`、`joining`；同 grant A 兑换 B 票据/使用 A proof 写 B 拒绝测试 | shared grant 不是安全身份隔离。若宿主不能可靠分离回调，必须独立绑定或限制执行成员 |
| 11 | 新增明确 team source、唯一 coordinator、source→唯一 Goal；跨重试键去重且个人委托不能重复接管 team source | `team_collaboration`、`chatroom`、`delegation`、`dot_chat`；公开团队来源/个人旧入口对抗 | 当前交互要求只 @ 唯一主责；多 @ 受控拒绝，无自动猜测团队 |
| 12 | 新增结构化 peer handoff、相邻 causation、深度上限、去环、目标和团队消息预算 | `team_collaboration.handoff`；同键改正文冲突、回传链成环拒绝 | 普通 bot 消息继续不触发 bot-to-bot 自动唤醒；新的独立业务交接仍受总预算约束 |
| 13 | 已有逐输入版本和handling；新增消费guard及权威team assignment/publication证据，source→work_linked→reply_committed完整收口 | `message_handling`、`dot_chat`、`team_collaboration`；公开双Dot发表、ack refs后两session yield成功 | 先前发表后source仍ready缺陷已由真实证据join修复；部分覆盖仍需逐输入显式登记，不推断自然语言已经覆盖全部请求 |
| 14 | 新增 team intent revision/coordinator epoch；所有重要消费写口须匹配当前 Inbox revision；已有 owner task context 保持 | `coordination`、`team_collaboration`、`dot_consumers.guard_work`；新输入先提交后 public execute 被拒绝且 operation 数为 0 | 语义决定由真实消费者在当前意图下完成；服务端版本屏障不自动解释任意自然语言取消 |
| 15 | 已有先停止新准入、按原 operation 请求取消及原回执对账；新增团队暂停/意图变更复用这些路径 | `coordination.stop_work`、任务活性/operation 恢复；`test_dot_task_liveness_recovery.py` | cancel ACK 不代表 OS 进程停止；真实 Agent 终态/断网恢复仍需独立验证 |
| 16 | 已有不可变 bounded execution budget；新增 consumer lease 不延长或伪造 work lease | `test_dot_bounded_execution.py`、`test_dot_bounded_execution_process.py`；consumer heartbeat/yield 测试 | 基线长进程保证需在冻结候选上回归，不能由会话单元测试代替真实进程测试 |
| 17 | 已有 operation 生命周期资源占用与未知结果隔离；新增会话/主责变更不重写原 operation | 既有执行/资源恢复测试；公开新输入拒绝准入测试；回滚工具保留 operation/锁 | 旧 fence 不会杀死 OS 进程；未确认终态的资源不得重新交给第二个 writer |
| 18 | 已有项目/SHA/资源边界；新增精确 work assignee Dot、当前 intent+approval+产物版本/digest 验证和汇总集合校验 | `coordination`、`team_collaboration.artifacts/publish`；团队旧产物/遗漏结果拒绝测试 | 不自动创建任意跨仓库 worktree或替用户解决语义冲突；资源清单仍须对应实际操作 |
| 19 | 新增当前coordinator与epoch；房主或当前主责可显式切换到已获准成员，旧主责上下文失效且原结果保留 | `team_collaboration.takeover`；跨grant接替、旧epoch拒绝 | 不声称自动故障选举；不授予未批准成员新权限，没有获准候选时需原房主决定 |
| 20 | 已有结果与渠道 outbox/receipt 分离；新增 team publication 幂等、当前结果集合绑定，不重做执行 | `team_collaboration.publish`、`channels`；公开两 Dot 结果→唯一原话题汇总 | 外部真实渠道投递失败/恢复需要渠道级证据；合成 panel 发表不是外部收到 |
| 21 | 新增typed Inbox分页、48KiB页预算及含request schemas的契约hash；过滤/字节截断保留游标；前端Goal首包40条、显式load more、迟到页隔离 | `dot_mailbox`、`collaboration-goals.js`；80重引用分页不漏项且每页≤48KiB，UI请求预算/重复游标测试 | 字节上限与按需读取已有证据；没有将其换算成未经测量的真实模型token降幅 |
| 22 | 已有 Store 单 worker 和短事务；新增定向索引、分块 ready 检查、Store 队列/事务有界聚合指标；新增 burst 测量 | `dot_mailbox`、`events`、`store`/metrics；`test_multidot_acceptance_load.py` | 本机一次 burst 不替代 15 分钟目标分布压测；没有逐 SQL 耗时 tracing 或数据库扩容结论 |
| 23 | 新增同源 SSE hint 合并、单飞+尾随同步、退避及慢轮询兜底；复用原位 DOM/IME/选区/滚动保护 | `relay_hints`、`collaboration-relay.js`；`test_multidot_ui_unit.py`、`test_multidot_ui_browser.py` | 真实目标手机输入法、键盘、INP 与滚动样本待测；桌面自动化不能替代 |
| 24 | 已有真实阶段回复/操作回执状态；新增 v2 health 和主责汇总状态，只有当前 intent 的 publication 显示最终结果 | `dots`、前端 relay/goals；状态与消费者回归 | 不增加假 token、假在线；宿主不能强制唤醒时显示已保存/等待并保留待办 |
| 25 | 已有单条阶段更新和 Goal 工具预算；新增团队累计 steps/messages、后台成员明细折叠、主责统一发表与恢复提示预算 | `team_collaboration`、`events`、前端 goals | 尚未声称模型 token 成本或外部模型计费预算已计量；真实每目标成本需宿主记录 |
| 26 | 新增扩展模块无循环依赖注册、三工具动作目录及严格参数、旧别名兼容和显式版本拒绝 | `public_collaboration`；三种 fresh-process import顺序、fixed/role catalog/actions/schema 检查 | 实际宿主旧目录缓存需要 Rescan；不以新高权限连接绕过缺失动作 |
| 27 | 已有发送/读取/执行实时 grant/project 校验；新增票据 grant+slot+generation、scope digest和 cursor失效；SSE access_changed 停旧流 | `dot_consumers`、`dot_mailbox`、`relay_hints`、前端 | 没有通过缓存复用旧权限；真实撤权传播时延需部署样本，但源码拒绝路径可隔离验证 |
| 28 | 加法迁移保留原ledger；新增默认只读的回滚准备，准确源/DB/行版本CAS后原子冻结slot/policy/Goal，重复apply也拒绝新增范围 | `scripts/prepare_multidot_rollback.py`、`test_events_multidot_rollback.py`（11项） | 工具仅准备并冻结，不启动旧二进制或恢复准入。真实旧二进制回滚未执行；先停全部Hub writer，禁止用旧DB快照覆盖ledger |

## 4. 独立审查发现与修复

这些问题来自模块交界，单个模块测试通过不足以覆盖。以下保留实际失败原因与对应修复；完成后的验证结果见第5节。

| 缺陷 | 修复与验证 |
| --- | --- |
| 新 bearer 字段未进入通用脱敏 | `wake_ticket`、`consumer_proof` 的结构 key 与赋值形式脱敏；原 delivery body 需要加密而非删票据 |
| 通用 mutation 会明文保存 begin 响应 | begin 使用专用持久状态，只存 hash+加密 secret；幂等重放绑定原票据/grant/generation/scope |
| 旧CPJ没有Dot记录时被guard误拒，混合页面又触发liveness空对象 | 精确服务器委托事实允许原CPJ兼容路径；Dot专用liveness只连接真实Dot。保持CPJ读取和原授权，混合team/v2/CPJ全query逐表快照无副作用通过 |
| bool 被 Pydantic 转成有效 fence/revision | 新 consumer/Goal/Dot 关键整数 strict 校验，raw guard 仍明确拒绝 bool |
| v2 失效或指定 legacy sibling 可能恢复旧写入口 | durable v2 选择不降级；所有旧别名和未映射工作入口均检查原服务器 binding |
| source 去重仅 team→personal 单向成立 | 个人 from_message 和底层 delegation.send 同样拒绝明确 team source |
| team dirty reference 与 mailbox 白名单不兼容 | 仅传递有界原对象引用，复杂团队状态通过目标读取；实际 v2 team 流程验证 |
| team mutation 幂等键没有冲突语义 | 原键不同输入拒绝；业务去重与请求幂等分别记录，不因新键重复创建原 Goal |
| public extension 注册但动作未进入目录 | fresh-process 枚举每个 extension，核对 schema、description、request映射及 owner-only动作不可公开 |
| 已领取team work在dot_recovery中缺失 | 已扩展同一恢复页的personal/team精确assignment引用；独立public claim→recovery读到原work且零写入通过，随后原结果/唯一发表/排空仍成立 |
| team_result 发表后原 source 仍 ready | 建立真实 work/结果证据关系，最终 ack控制/结果提示后两名消费者能 yield；不靠改显示状态掩盖 |
| 新密文字段未加入 key rotation | 已将新proof/ticket列及混合明文/密文delivery body纳入轮换；真实begin后两次轮换及重开库仍可读，原投递体字节保持不变 |
| 直接回退旧代码会绕过 v2 fence | 已新增默认只读、精确CAS、原子冻结及幂等清单；重复apply重新检查范围，新增binding/source拒绝旧计划 |
| 原订阅过期后同grant可换callback接管原slot | 永久绑定原subscription identity，原URL同ID续订兼容，跨URL替换拒绝 |
| token bucket补充后仍等待固定2秒扫描 | lifecycle改用真实预算补充截止；同场景合成callback p95观测从2293.815降至584.105ms |

## 5. 可重跑验收与证据边界

独立验收文件：

- `tests/test_multidot_acceptance_consumers.py`：实际 service/schema/runtime/public tools，内存 native challenge/callback。覆盖 ticket→begin→原话题回复、秘密不在普通查询或明文数据库、跨 slot、旧写口、过期、dirty race、严格整数、catalog、team/source/交接/工作/发表与消费排空。
- `tests/test_multidot_acceptance_load.py`：十个Dot、一百个并发输入尝试、固定十个订阅、一个50ms合成慢端点；现有发言限速明确接纳60并拒绝40；检查Store队列、callback并发和全部已接纳输入持久保留，输出匿名阶段测量。
- `tests/test_multidot_acceptance_evidence.py`：离线 manifest 的版本、证据摘要、场景完整性、路径边界和模拟/真实声明分离。
- `test_collaboration_delegation_consumer.py`中的全query用例：真实CPJ委托、team来源/Goal、native验证的v2绑定与活跃consumer共存，严格覆盖所有公开query动作，逐动作比较包括team_*在内的业务表快照。

基础执行方式（从候选 worktree，单进程）：

```sh
PYTHONPATH=. /path/to/python -m pytest -q -p no:cacheprovider --basetemp=.work/multidot-acceptance tests/test_multidot_acceptance_consumers.py tests/test_multidot_acceptance_evidence.py
PYTHONPATH=. /path/to/python -m pytest -q -s -p no:cacheprovider --basetemp=.work/multidot-load tests/test_multidot_acceptance_load.py
```

| 层级与执行目录 | 结果 | 原操作回执 | 范围 |
| --- | --- | --- | --- |
| 隔离候选完整 fast | **5960 passed，3518 deselected，15 warnings，637.58s** | `[已脱敏标识]` | `not browser and not slow`，全部所选测试完成 |
| 前端定向批次 | 27 passed / 7.48s，其中12项浏览器、15项纯单元 | `[已脱敏标识]` | Chromium与WebKit；前端源码冻结时执行，未作为物理手机或native宿主证明 |
| 隔离host-schema HTTP | 1 passed / 3.72s | `[已脱敏标识]` | 真实临时Hub/Agent进程与HTTP；受限importer及外部Dot为fixture |
| 全仓Ruff | passed | `[已脱敏标识]` | 当前仓库配置 |
| mypy | passed | `[已脱敏标识]` | 当前配置的3个文件，不代表全仓类型检查 |
| 前端Prettier | passed | `[已脱敏标识]` | 全部前端脚本/CSS、core及mcpapps |
| diff与构建一致性 | passed，outdated:none | `[已脱敏标识]` | `git diff --check`、`build_web_assets --check` |
| 原MCP工作目录smoke | **20 passed，9466 deselected，9 warnings，10.87s** | `[已脱敏标识]` | 代码写回后执行；保留原有8个无关修改，不以隔离结果替代原目录检查 |

最终fast后重新生成变更集清单，63个代码文件的指纹仍为页首值。15条warnings来自测试框架弃用/API兼容及JUnit属性报告告警；本批没有失败或中途停止。

不同批次存在重叠，不将数字相加。fast 是明确选择的层级，3518个deselected不由该批提供通过证据；另外执行的browser/slow项按各自回执报告。UI批次的15项纯单元包含在后续fast中。原目录smoke只验证该20项集成行为与原有修改共存，不等同于在原目录重复全部隔离回归。

首次全仓快速门禁遇到执行环境的测试目录问题：CodePier project_clean的TMPDIR位于受保护的Agent内部state目录，导致测试创建项目时被既有PROTECTED_ROOT保护拒绝。未放松保护或修改运行时配置，使用pytest正式的`--basetemp=.work/...`选择隔离测试目录后，原失败模块23项通过并重新运行整批门禁。失败试跑不算完整通过证据。其后一次正式门禁在1080项通过后发现旧CPJ委托slot没有Dot记录而被新消费者guard拒绝的实际兼容回归；该轮也不算完整通过，必须修复生产判断并保持旧测试断言后重跑。

后续门禁在1269项通过后暴露共用契约样例字典遗漏新增动作的18个必需字段。仅补齐`tests/test_public_collaboration_contracts.py::VALUES`；保留每个动作的严格required字段删除拒绝、object-only宿主导入和精确catalog断言，没有修改生产契约迎合旧样例。这是目录扩展后的fixture维护，区别于上述CPJ实现回归。补齐后public contracts、host schema和16项独立公共验收共197项通过；既有真实隔离Hub/Agent HTTP流程另1项通过。HTTP里的受限schema importer和外部Dot仍为测试fixture。

Events协议兼容测试同样保持精确事件目录、cursor与retention断言：目录新增v2事件；历史数据的插入和回填时间在同一事务完成，纯协议fixture关闭并不负责测量的后台发送循环，避免提交后唤醒先于测试保留期构造。完整该文件66项通过；真实投递泵另由独立调度和HTTP流程覆盖。

完成全部所选测试的一轮门禁`[已脱敏标识]`记录为3失败、5955通过、3518 deselected（646.65s），并非通过收据。余下三处分别核实为：新v2事件发现测试缺独立schema分支；旧密钥轮换fixture假设所有新密文表都有`id`主键；测试分类器按filename永久缓存，pytest删除成功case目录后复用同路径导致旧分类污染。前两处维护原严格契约断言，第三处修分类器为读取当前源码、最多512项纯源码分类缓存，并新增同路径改写/删除重建回归，原轻量helper应判`(False,False)`的期望保持。分类/回归计划模块65项通过。事件发现模块2项通过（`[已脱敏标识]`），密钥相关三模块29项通过（`[已脱敏标识]`）。随后完整重跑的最终fast结果见上表；这轮失败批次保留为修复证据，不与通过数相加。


## 5.1 本机负载实测

环境为 Darwin/arm64、Python 3.13.12、SQLite WAL，独立临时数据库和内存 callback。接入阶段只用虚拟时钟跨过原有六次challenge/分钟窗口；实际测量阶段使用真实wall clock与monotonic计时，未跳过发言限速、投递预算或队列。

固定场景：10个Dot、10条原生订阅、100个并发owner发言请求、1个50ms合成慢端点。现有发言门禁接纳60条、明确返回40个MESSAGE_RATE_LIMIT；60条来源和60个Inbox引用全部保留，并合并为10个提示。10次consumer_begin均产生服务器消费回执；未创建任何真实执行operation。

| 指标（毫秒） | 固定2秒恢复扫描样本 | 按真实预算补充截止样本 |
| --- | ---: | ---: |
| 100次输入尝试完成 | 485.047 | 462.062 |
| 已提交事件→合成callback p50 | 102.163 | 106.281 |
| 已提交事件→合成callback p95/p99 | 2293.815 | 584.105 |
| dispatch排空阶段 | 2264.723 | 585.237 |
| begin请求→服务器receipt p50 | 20.924 | 21.721 |
| begin请求→服务器receipt p95/p99 | 39.856 | 39.972 |
| Store队列等待 p50 | 28.008 | 24.867 |
| Store队列等待 p95 | 480.474 | 457.769 |
| Store执行phase p95 | 10.156 | 12.16 |
| SQLite最外层事务 p50 | 2.315 | 2.13 |
| SQLite最外层事务 p95 | 10.079 | 12.143 |

第一份样本操作：`[已脱敏标识]`。第二份样本操作：`[已脱敏标识]`。第二次使用生产`next_dispatch_delay(2)`返回的116.302ms和182.472ms等待；没有增加测试专用高频轮询。两次观测表明固定扫描是这个突发场景的尾延迟来源，修复仍保留次数/字节预算。

第二份样本Store worker：测量开始前（已完成setup）已有54个已完成phase，测量结束198个；最近198个队列/phase样本包含setup。事务样本从73增加至217，包含startup迁移；40次rollback和worker failed均对应明确的发言限速拒绝，commit_failed/rollback_failed为0。瞬时peak_pending=100，结束queued=running=0。callback实际峰值为2，单端点峰值为1，未超过全局4/端点1上限。

百分位使用nearest rank，callback和begin各仅10个样本，因此p95/p99等于该批最大值。Store分位数是最多2048个最近样本，不是before/after分位数之差。事务计时截止于commit/rollback返回，排除after_commit回调。

这两次本机单次突发测试不构成生产SLO、模型响应延迟或手机INP证明。队列p95约458ms反映100个请求同时进入单worker，不能当作持续15分钟超过100ms的数据库迁移触发证据。真实负载分布、持续压力、跨owner竞争和实际宿主仍应独立测量。

## 6. 真实宿主与真实设备门禁

尚未执行以下真实场景：

1. 原宿主 native Events →真实 Dot 自动读取→原处回复的普通双向闭环。
2. 原房主批准范围内的真实可恢复操作，结果回到原话题且不重复副作用。
3. 两名真实 Dot 在同一 Goal 下分工、交接指定版本产物并由当前主责汇总。
4. 响应丢失、Hub/宿主重启与网络中断后，按原 operation/publication key 恢复。
5. 真实 native 续订跨过到期窗口；暂停/撤权停止新准入，恢复原 Inbox。
6. 目标手机 IME、键盘、滚动、重连及输入延迟；新 DB 上的实际旧二进制回滚。
7. 至少15分钟的持续压力、长期内存观测及完整进程故障矩阵；本机短burst只验证本次明确定义的合成场景。

`verify_multidot_host.py` 的输出即使 `ready_for_review=true`，也始终保留：

```json
{"host_independently_verified": false, "grants_authority": false}
```

它只检查清单声明、文件内容摘要和场景要求是否完整，供人工核对真实回执。不能把离线文件验证、mock、隔离浏览器、真实宿主或生产灰度互相替代。

## 7. 发布与回滚执行顺序

1. 冻结候选源码，生成与实际候选一致的源码指纹。
2. 完成所需静态检查、定向回归、构建和隔离完整回归；全量结果按 `docs/DEVELOPMENT.md` 要求检查未执行/跳过/超时及源码变动。
3. 在受控测试环境收集上述真实宿主、设备和恢复证据；没有证据的项保持未验证。
4. 回滚前运行只读计划，核对精确 slot/policy/Goal 和当前版本；只有应用后的冻结回执成立，才允许切回旧二进制。
5. 保留原 operation、资源占用、结果、outbox、消息和新表。查询旧操作终态，不重新派发原副作用，不用旧数据库覆盖当前 ledger。

本次未对生产环境运行恢复、回滚、迁移开关、native订阅或部署操作。
