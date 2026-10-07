# 协作中心：只读试点使用与运维说明

协作中心让项目讨论、目标、任务、探针证据和智能体结论共用持久记录。面板指令、MCP 建议和监控异常进入同一套权限、预算、租约和 outbox 服务。

本次代码默认关闭，未替任何真实 Work/dot 聊天订阅事件，也未启动生产采集。完整评审、相对 v0.1 的修订和未交付范围见 [实施设计 v0.2](designs/COLLABORATION_V0_2.md)。

## 1. 首期包含什么

独立侧栏入口「协作中心」，包含讨论、任务、监控、智能体与订阅四个视图。支持明确选择收件智能体、同名区分、来源消息防重、MCP 建议等待面板批准、一次明确的分析到 dot 汇总交接。任务与证据详情内联展开，桌面和窄屏共用记录。

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
| COLLABORATION_ENABLED | 主开关。开启共享面板和九个协作工具；子开关不得在主开关关闭时独立开启 |
| MCP_EVENTS_ENABLED | 开启现代 MCP 的事件目录、订阅和 webhook 投递 |
| MONITOR_COLLECTOR_ENABLED | 允许已批准、未过期、未暂停计划的固定 HTTP 探针采集 |
| ANALYSIS_DISPATCH_ENABLED | 允许监控创建分析任务，并将正常任务可用事件投递给消费者 |

只开主开关即可试用讨论室和手动领取。关闭事件或分析派发时，面板显示「待人工领取」，不会伪装为模型已被唤醒。订阅测试事件可以在分析派发关闭时验证路由，但仍需 Events 主功能和有效授权。

四个开关都不包含代码写入、Shell、安装依赖、部署或服务重启能力。启用它们也不能绕过具体项目授权。

## 3. 初始化一个隔离试点

先由空间管理员选择一个有授权的测试项目，在协作中心创建 production 或明确命名的测试环境讨论室。创建讨论室本身不采集任何目标，也不创建凭据。

为 Work 和 dot 准备可区分的现有 CodePier 只读连接，项目范围仅含试点项目。当前 scopes 必须包含 read，只能额外包含 devices.read；带 write、execute 或其他管理权限的连接会被拒绝登记。先在目标聊天核对 get_access_context，再在面板「智能体与订阅」选择该连接登记用途，默认有效七天。

用户分别在真实目标聊天主动请求以下四条订阅。平台负责生成回调与签名材料，不在聊天、讨论室或截图中粘贴秘密。

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

任务类型限定：propose_monitor_plan、analyze_incident、summarize_result。不存在 arbitrary_command。面板发送记录是经当前会话认证的用户指令；MCP collaboration_command_create 只保存待批准的 agent_proposal，不能自己声称获得用户授权。管理员在同一讨论室批准后才创建目标和任务。

消费者先读取当前任务，再领取其准确版本：

```json
{
  "project": "<当前授权项目 ID 或工具支持的别名>",
  "environment_id": "production",
  "job_id": "<读取到的任务 ID>",
  "expected_version": 1,
  "idempotency_key": "claim:<稳定请求标识>"
}
```

通过 collaboration_claim 获得 attempt、fencing_token、lease_until、deadline_at 和 context。租约最多十五分钟，任务总期限最多三十分钟；阶段性进展可用 collaboration_heartbeat 延长，但不能越过原期限。

collaboration_result 提交结构化分析，必须携带原 job_id、attempt、fencing_token 和稳定幂等键。已成功提交后重试同一请求会返回原结果；不同内容不能覆盖同一轮结果。旧租约结果只留作 late_result，不改变当前任务或触发交接。

重要事实放 observations 并引用有效 evidence_refs；未证实因果关系放 hypotheses。outcome 可为 healthy、explained、action_required、blocked、inconclusive。healthy/explained 没有证据会被拒绝。恢复由监控规则决定，不由模型回合结束决定。

证据读取使用 collaboration_read(kind=evidence)，Work 提供有效任务租约字段；dot 通过绑定的 result_id 读取该结果引用的证据。结果消费者可调用 collaboration_ack，表示业务已处理，不表示用户已读。

遇到缺权限、缺资料、平台拒绝，调用 collaboration_block 并停止；不能更换身份绕过拒绝。

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
