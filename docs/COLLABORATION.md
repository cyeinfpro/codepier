# 协作中心使用与运维说明

> 新的任务 dot 默认走 **添加 dot → CPD 加入码 → 一条宿主任务订阅 → @ 交办 → 原话题回传**，见 [dot 接入与验收指南](DOT_COLLABORATION.md)。下面的 CPJ、监控通知和通用发言说明继续适用于旧用途，不是新 CPD 接入的额外步骤。

> 1.20 聊天式主屏、多项目房间、目标驱动协作和连接流程见[聊天室指南](COLLABORATION_CHATROOM.md)。下文保留原有只读任务、监控、四类业务事件和 CPJ 接入的运维边界；新聊天提醒与目标通知单独订阅，不改变既有路由。

协作中心让项目讨论、目标、任务、探针证据和智能体结论共用持久记录。明确转为任务的面板请求、MCP 建议和监控异常进入同一套权限、预算、租约和 outbox 服务。

本次代码默认关闭，未替任何真实 Work/dot 聊天订阅事件，也未启动生产采集。完整评审、相对 v0.1 的修订和未交付范围见 [实施设计 v0.2](designs/COLLABORATION_V0_2.md)。

## 1. 首期包含什么

独立侧栏入口「协作中心」，以讨论消息为主，任务、监控和成员连接按需查看。支持明确选择收件智能体、同名区分、来源消息防重、MCP 建议等待面板批准、一次明确的分析到 dot 汇总交接。任务与证据详情内联展开，桌面和窄屏共用记录。

服务端提供不可变监控草稿、独立批准、固定 GET 探针、合成探针可达性/错误比例/p95、连续窗口确认、恢复探针、维护通知抑制、有限重试和异常复发。监控运行在长期在线且能够访问目标的 Hub 上，不依赖个人电脑持续保持聊天。

MCP Events 服务端提供事件发现、签名 challenge、订阅续期与轮换、加密回调存储、事务 outbox、私网回调拒绝及游标恢复。它不能创建任意 ChatGPT 聊天、选择聊天模型、读取私人聊天历史或保证模型按固定时间响应。

## 2. 功能开关

维护者在已经获准的部署变更中设置以下环境变量；修改源码或推送 GitHub 不等于运行中的 Hub 已经升级。

```dotenv
CODEPIER_COLLABORATION_ENABLED=false
CODEPIER_MCP_EVENTS_ENABLED=false
CODEPIER_MONITOR_COLLECTOR_ENABLED=false
CODEPIER_ANALYSIS_DISPATCH_ENABLED=false
```

| 开关 | 作用 |
| --- | --- |
| COLLABORATION_ENABLED | 主开关。开启共享面板和三个协作工具入口；子开关不得在主开关关闭时独立开启 |
| MCP_EVENTS_ENABLED | 开启现代 MCP 的事件目录、订阅和 webhook 投递 |
| MONITOR_COLLECTOR_ENABLED | 允许已批准、未过期、未暂停计划的固定 HTTP 探针采集 |
| ANALYSIS_DISPATCH_ENABLED | 允许监控创建分析任务，并将正常任务可用事件投递给消费者 |

只开主开关即可试用讨论室和手动领取。关闭事件或分析派发时，面板显示「待人工领取」，不会伪装为模型已被唤醒。订阅测试事件可以在分析派发关闭时验证路由，但仍需 Events 主功能和有效授权。

四个开关都不包含代码写入、Shell、安装依赖、部署或服务重启能力。启用它们也不能绕过具体项目授权。

## 3. 直接订阅或使用加入码

在已经连接 CodePier 的目标 Work/dot 聊天中，用自然语言指定项目、环境、事件和收到通知后的动作，即可请求宿主创建订阅。现有连接只要仍有该项目的 `read` 权限就可以订阅；同时拥有 `write`、`execute` 不再阻止通知接入。无需为纯通知另建凭据或先登记智能体。首次订阅只会为同一空间、用户、项目与环境建立被动记录容器，不创建任务、智能体或监控计划。

宿主必须实际提供回调与签名材料并完成验证。事件仍按项目、环境、队列及定向 grant 过滤；每次投递重新验证当前授权，移除读取权限、项目访问权或撤销连接后停止。订阅不代表自动领取任务，事件内容不是用户新指令，也不会给现有连接增加任何权限。相同高权限凭据仍有原来的交互工具能力，服务端不宣称它在事件唤醒回合自动降权。

例如：在此聊天订阅 MCP 项目 production 环境的异常状态通知；收到后只汇总状态和需要我决定的事项，不执行命令或改配置。使用实际项目 ID；成功必须以宿主真实订阅和预期聊天收到测试事件为准。

### 面板建房间 → 独立加入码 → 宿主订阅 → 聊天收件确认

1. 在获准项目环境创建讨论室，打开「智能体与订阅」。为每个 dot 或 Work 聊天分别创建位置并命名；同名位置也有不同编号，不把一个码复用为两个聊天。
2. 复制完整加入指令，在目标聊天中提及 CodePier 插件并发送。加入码默认 30 分钟有效，可在未兑换时刷新，旧码立即失效。位置默认有效七天。加入码只定位房间和位置，不授予项目权限、不创建 grant、不提升 read/write/execute 权限。
3. `collaboration(action="join")` 重新核对现有连接的用户、Space、项目和授权世代。含 read 的已有连接可以登记，成功后返回带 `slot_id` 的订阅请求。此时仅完成登记，不创建任务、工作者身份或回调签名材料。
4. 目标聊天的宿主按其确认流程创建订阅。必须使用 ChatGPT 网页的 Work、桌面端选择 Cloud 的 Work，或 dot；本地/SSH Codex 会话可以调用登记工具，但没有原生事件订阅能力。Work 位置请求任务与组件状态两类事件；dot 另请求结果与异常变化，共四类。面板显示 N/M 和缺项。插件页看不到新工具或事件时，可按[官方测试流程](https://developers.openai.com/plugins/build/mcp-events#test-in-chatgpt)重新扫描 MCP。已登记而缺少订阅的位置提供「复制继续订阅指令」，加入码过期后也可在原位置有效期内继续，不需要再次兑换。
5. 宿主回调通过 challenge 后，面板显示已验证的订阅。发送测试，在目标聊天核对每个测试事件编号。HTTP 2xx 仅证明接收端接受 webhook，不证明目标聊天处理或用户已读。
6. 实际看到测试后勾选收件确认，记录为明确的用户确认，仍不声称密码学聊天身份。只有要求的事件集合完整、全部有效路线都确认，才显示完整接通。缺项和部分确认单独展示。签名轮换、过期后重新订阅要求新测试，不复用旧回执证明新世代。

已兑换码到期后，只有原成功请求的原幂等键可以恢复回执，并继续重验当前权限、位置有效期与撤销状态；新键兑换拒绝。重复调用不证明另一个聊天身份。不同连接不能接管已登记位置；换聊天或连接请撤销旧位置并新建。

### 插件可见事件，但仍是 0/N

插件详情页显示事件列表证明 `events/list` 发现成功；它不证明宿主自动化服务已经接受该连接器。先让原 dot / Work Cloud 读取当前可订阅事件源，使用返回的实际 `connector_id`，不能从插件 ID、显示名称或旧会话缓存推导。如果宿主报告「创建前无法验证 webhook 连接器」且服务端没有收到 `events/subscribe`，故障发生在宿主订阅创建之前。记录插件 ID、发生时间与具体错误用于平台排查；不要反复换加入码、伪造回调或手工把数据库状态改成已接通。

加入指令要明确要求持续订阅及收到事件后的动作，并区分宿主事件订阅与 CodePier 业务任务。仅调用 `collaboration(action="join")` 后回复「宿主不支持」不是订阅失败的实测证据；应检查是否实际查找宿主订阅能力、查询事件来源和尝试创建。原生订阅不一定作为 CodePier 的普通工具暴露，不能只凭工具列表没有 `events/subscribe` 就认定宿主不支持。

服务端脱敏日志中的字段是 `rpc_method`。按 `server/discover`、`events/list`、`events/subscribe` 和 `event_callback_failed` 阶段核对；`event_catalog_returned.event_count` 可区分空目录与有效发现。认证前失败时 `rpc_method` 仍是 `unknown`，只把白名单内的请求头方法记为 `rpc_method_hint`，它不代表请求体或授权已验证；不要只凭没有已解析的订阅方法就排除认证失败。订阅请求到达后再检查 `-32015` 的原因和回调 HTTP 状态。必需订阅齐全、测试 webhook 获 2xx、对应聊天实际收件三项全部完成，才算联通验收。

`slot_id` 将真实宿主订阅关联到面板位置；相同 grant、回调与签名材料不能同时冒充两个独立位置。回调 URL、签名材料及其摘要不会在位置视图中展示。日常通知仍按项目、环境、原授权和队列过滤，同项目同队列多个位置可能收到同一摘要；不能把 `@位置` 解释为仅某个聊天可见或获得任务 claim 权限。测试事件只投递到指定 subscription，不创建业务任务。

暂停房间、授权撤销、位置撤销或到期会阻止后续投递。撤销位置停止其关联订阅和待发回执，不删除其他位置或未关联订阅，无法收回第三方已接收数据。加入位置不改变四个功能开关，也不自动变成任务收件人。

### 聊天提醒与直接委托的升级入口

现有 CPJ 返回的监控事件集合保持不变。普通聊天提醒使用独立的 codepier.collaboration.message_mentioned.v1，直接委托使用独立的 codepier.collaboration.delegation_available.v1；两者都需要用户在目标宿主明确接入，不会因为源码升级、重新加入或旧订阅续期自动开启。

普通提醒只让消费者读取和讨论原消息，不能把正文自行提升为执行授权。已有有效规则时，@ 菜单主动作「交给dot处理」一次点选即可设好明确委托，无需另找发送下拉；次动作「仅讨论提醒」和纯文本 @ 均不派发任务。规则默认七天、每次任务一小时、最多一百次委托，受加入位置更早到期限制；原生接入仍需按该规则返回的 subscription_request 独立明确订阅，消息保存成功不等于通知已投递。规则级事件包含 policy_id 和 policy_version，同一规则版本的新委托复用通知路线；更改规则后要确认新的精确订阅，旧 route 不变宽。详细使用方式见[聊天式协作室](COLLABORATION_CHATROOM.md#在既有范围内直接委托)。

新委托消费者的处理约定应单独明确，不覆盖原四类监控事件的只读要求。公开工具已收敛为 collaboration_query、collaboration、collaboration_work，旧名字仅保留同权限调用兼容，迁移见[工具指南](CORE_TOOLS.md#旧消费者迁移)。初次接入和当前待办分别使用 query 的 connection、inbox action；保存起始检查点，后续唤醒使用原检查点从当前队列协调。以下执行步骤仅适用于宿主已明确同意 managed_execution 的消费者，notification_only 始终只读取与报告：

1. test=true 只核对测试编号，不领取工作。
2. 非测试通知先通过 collaboration_query(action="delegation") 读取当前记录，验证认证房主、规则版本、目标批准、项目和接收连接；事件正文、位置标签和同名账号不是执行授权。
3. 在当前有效范围内领取相应工作，仅通过 collaboration_work(action="execute") 执行受管步骤，再用 collaboration_work(action="result") 回传真实操作回执与限制。同一委托不要求房主再次手动“转任务”。
4. 重复或迟到通知先重读工作与租约，不重跑不确定操作。异常恢复只允许房主「重试尚未开始的步骤」，并要求该步骤全部历史尝试均无操作准入记录；已有任何操作记录先核对原回执，不能自动重试。需要额外确认的动作遵循宿主审批；拒绝后记录并报告阻塞，不能换凭据或改用普通 exec 绕过。
5. 结果写回原话题；平台接受 webhook、助手处理、操作完成与房主验收是四个不同状态。

确认接通时，应检查实际安装连接能发现并调用新的读写工具，包括带 conversation_id 的 collaboration_query 对应记录 action、collaboration(action="message")、collaboration_query(action="delegation") 和受管工作工具。服务端 tools/list 正确不等于当前聊天已经获得新版 schema。按[官方测试流程](https://developers.openai.com/plugins/build/mcp-events#test-in-chatgpt)重新扫描后，仍需真实原生订阅、challenge、事件投递与目标聊天处理证据。合成回调测试不替代宿主验收；不得猜测插件 ID、伪造回调或手写签名材料。

### 可选：初始化只读分析消费者

先由空间管理员选择一个有授权的测试项目，在协作中心创建 production 或明确命名的测试环境讨论室。创建讨论室本身不采集任何目标，也不创建凭据。

为 Work 和 dot 准备可区分的现有 CodePier 只读连接，项目范围仅含试点项目。当前 scopes 必须包含 read，只能额外包含 devices.read；带 write、execute 或其他管理权限的连接会被拒绝登记。先在目标聊天核对 get_access_context，再在面板「智能体与订阅」选择该连接登记用途，默认有效七天。

下面的只读消费者登记仅供领取/分析任务；纯通知不需要。用户分别在真实目标聊天主动请求以下四条订阅。平台负责生成回调与签名材料，不在聊天、讨论室或截图中粘贴秘密。

| 聊天用途 | 事件 | arguments |
| --- | --- | --- |
| Work 分析 | codepier.collaboration.task_available.v1 | project_id、environment_id、queue=work-analysis |
| dot 协调 | codepier.collaboration.task_available.v1 | project_id、environment_id、queue=dot-coordination |
| dot 结果 | codepier.monitor.result_ready.v1 | project_id、environment_id |
| dot 异常状态 | codepier.monitor.incident_changed.v1 | project_id、environment_id |

project_id 必须是实际项目 ID，不使用显示别名。可额外订阅 codepier.monitor.status_changed.v1。订阅到期或授权失效后不继续投递；面板可暂停订阅，平台自动续订不能覆盖用户暂停。

在面板对不同订阅分别发送无敏感测试事件。必须实际确认事件进入预期聊天，而不是只看到 HTTP 2xx。测试数据包含 test=true，不对应真实业务任务，消费者应忽略领取。没有真实聊天响应证据时，P0 平台验收仍未完成。

同一个 grant 中的两个聊天没有服务端可证明的聊天级隔离。登记标签不是安全身份。强隔离需服务端能区分的连接与实际产品选择验收。

## 4. 下达指令和处理结果

在讨论页选择「@ 收件智能体」，填写明确请求、验收条件和固定任务类型。普通文字里的 @ 只是内容，不自动添加接收者。可明确指定另一位 dot 作为一次结果汇总的下一步，默认不自动交接。

任务类型限定：propose_monitor_plan、analyze_incident、summarize_result。不存在 arbitrary_command。面板发送记录是经当前会话认证的用户指令；MCP collaboration(action="command") 只保存待批准的 agent_proposal，不能自己声称获得用户授权。管理员在同一讨论室批准后才创建目标和任务。

消费者先读取当前任务，再通过 collaboration_work 的 analysis_claim action 领取其准确版本：

```json
{
  "action": "analysis_claim",
  "project": "<当前授权项目 ID 或工具支持的别名>",
  "environment_id": "production",
  "job_id": "<读取到的任务 ID>",
  "expected_version": 1,
  "idempotency_key": "claim:<稳定请求标识>"
}
```

通过 collaboration_work(action="analysis_claim") 获得 attempt、fencing_token、lease_until、deadline_at 和 context。租约最多十五分钟，任务总期限最多三十分钟；阶段性进展可用 collaboration_work(action="analysis_heartbeat") 延长，但不能越过原期限。

collaboration_work(action="analysis_result") 提交结构化分析，必须携带原 job_id、attempt、fencing_token 和稳定幂等键。已成功提交后重试同一请求会返回原结果；不同内容不能覆盖同一轮结果。旧租约结果只留作 late_result，不改变当前任务或触发交接。

重要事实放 observations 并引用有效 evidence_refs；未证实因果关系放 hypotheses。outcome 可为 healthy、explained、action_required、blocked、inconclusive。healthy/explained 没有证据会被拒绝。恢复由监控规则决定，不由模型回合结束决定。

证据读取分别使用 collaboration_work(action="job_evidence") 或 collaboration_query(action="result_evidence")；Work 提供有效任务租约字段，dot 通过绑定的 result_id 读取该结果引用的证据。结果消费者可调用 collaboration_work(action="ack")，表示业务已处理，不表示用户已读。

遇到缺权限、缺资料、平台拒绝，调用 collaboration_work(action="analysis_block") 并停止；不能更换身份绕过拒绝。

## 5. 配置只读监控

在「监控」中登记固定 HTTP GET 目标，确认它不会修改业务数据，URL 不含凭据或客户内容。内部服务需要填写明确允许的 CIDR；明文 HTTP 必须单独勾选，不能借此把私网回调放行。

选择入口与恢复探针后，可生成初始 JSON，再按项目真实基线调整阈值、窗口、样本量和到期时间。点击「只校验」不会保存；点击「保存新草稿」不会激活。需要审阅最新不可变版本及固定目标，勾选明确批准范围，再激活。

计划最长三十天，变更产生新版本。正在执行的原版任务不会继承新版授权。旧计划异常不会因更改阈值被静默关闭，改版前应核对未结束异常。

这些信号是合成探针观测，不是实际全部用户请求、主机指标、数据库健康或完整业务链路。Hub 采集器未开启、数据过期、样本不足、恢复探针失败都会明确显示，不能解释为业务已恢复。

## 6. 停止、故障与重试

| 面板状态/动作 | 真实含义 |
| --- | --- |
| 已排队 | 任务在库中；不证明事件已经发出 |
| 事件已接收 | 回调返回 2xx；不证明消费者已领取 |
| 已接收但尚未领取 | 接收后超过试点等待窗口仍无 claim |
| 已领取/正在处理 | 当前有效租约，仍不证明结果完成 |
| 结果已提交 | 分析结果已持久化；业务异常可能仍未恢复 |
| 探针已验证恢复 | 对应规则的持续恢复和新鲜探针条件满足 |
| 暂停协作室 | 阻止新工作并使运行中租约失效；重新开始需原期限内明确处理 |
| 暂停计划 | 不再采集此计划；不会删除原证据 |
| 暂停订阅 | 暂停投递，不等于协议退订；恢复要经当前授权复核 |
| 取消只读任务 | 停止内部任务，不声称已终止任何外部进程 |

只读执行超时有限重试，原总期限、预算和最大三次领取仍有效。授权撤销、用户停止和 blocked 不自动换身份重试。回调 410/413 进入需要检查的投递失败，不原样无限重试，也不伪造退订。

单个项目不同环境共用预算，默认最多两个未结束任务、每队列一个有效租约、每小时六个/每天二十个新分析。任务调用预算针对协作服务能核验的调用，不声称统计所有 ChatGPT 内部 token 或现有任意只读工具调用的精确费用。

## 7. 运维限制与恢复

采样默认保留七天，证据正文默认十四天，过期证据保留可识别墓碑。首期尚未自动归档全部消息、结果、审计与 outbox 历史；须观察数据库增长，不应未经容量验收直接扩为无限期、多项目生产监控。分布式 Agent 本地缓冲、主机采集与生产请求聚合未实现。

备份必须包含数据库与配套加密材料；新订阅密文参与已有密钥轮换。恢复旧快照或回滚前，先将四个开关全部关闭，核对原任务和投递水位后再启用。当前没有自动证明任意旧快照安全的恢复检测器，不允许将恢复流程省略为直接重启。

升级/部署、真实订阅、真实采集、真实生产故障演练分别需要相应授权。本次提交仅新增源码和测试，不代表线上生效。

## 8. 开发验证

使用项目已有完整开发环境，未新增 Python/Node 依赖：

```sh
.venv/bin/python -m pytest tests/test_collaboration_service.py tests/test_collaboration_events.py tests/test_collaboration_monitor.py tests/test_collaboration_http.py -q
.venv/bin/python -m pytest tests/test_collaboration_browser.py -q
npm --prefix web/mcp-apps run format:check
npm --prefix web/mcp-apps run build
.venv/bin/python scripts/build_web_assets.py --check
```

浏览器测试以临时 Hub、合成项目和独立 Chromium/WebKit context 执行；事件测试用本地可控接收器。它们不会调用真实模型 CLI、创建真实 ChatGPT 订阅或替代真实 Work/dot PoC。

主要代码位置：hub/collaboration、shared/collaboration_contracts.py、web/collaboration.js、web/collaboration.css。认证、工具目录、加密轮换、数据库与应用生命周期均复用原项目入口。
