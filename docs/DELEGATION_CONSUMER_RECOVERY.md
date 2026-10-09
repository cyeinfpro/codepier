# 面板委托到宿主执行的恢复验收

面板的明确房主委托先保存原消息、不可变允许范围与受管工作项。事件只负责唤醒宿主；由宿主调用 CodePier 工具处理该项目，再以真实操作回执回复原话题。Hub 不在后台代替模型执行消息正文。

## 接入保存什么

1. 在面板选择精确项目、接收位置、用途、能力及具体项目 Agent / VPS。候选 VPS 来自该项目的实时绑定；已停用或未严格核对主机身份的连接不可选择。发现候选不会修改旧策略，增加目标仍需明确确认新版本。
2. 在宿主确认 notification_only 或 managed_execution。升级程序和复制说明不升级旧的仅通知消费者。
3. 仅首次接入调用 connection，取得初始 signed checkpoint、精确 subscription_request 和 consumer_configuration。
4. 在宿主持久消费者中保存 consumer_configuration.wake_instructions（它包含精确 inbox 请求及初始基线），再使用原生订阅。不能只保存一段没有 checkpoint 的静态接入说明。
5. 每次唤醒优先使用已保存的 inbox 请求。即使宿主未带入事件 data，也可只读恢复精确策略的当前待办。结束前从首屏重新协调；分页游标不是永久消费进度。

丢失初始基线时不能猜测原值：重新接入建立新的报告基线，现存任务只报告。房主明确重新派发后，才可在有效范围内处理旧任务。

## 旧工具目录兼容

优先重新扫描插件，发现 collaboration_query / collaboration_work。旧工具目录尚未刷新时，服务返回 legacy_read_request、legacy_inbox_request、legacy_next_page_request、legacy_resume_request 及每项的 legacy_read_request / legacy_claim_request。只使用实际已发现的同一连接工具及原样参数。

旧版只读桥接使用已存在的 collaboration_read：kind=delegation_policies，id 为精确 policy，query 为版本化的 delegation-consumer-v1:connection|inbox:mode:version 选择器，after 为初始 signed checkpoint，cursor 仅用于临时分页。它只允许 connection/inbox 读取，仍执行原有项目、房间、连接、策略版本和签名检查，不提供任意 RPC 或执行入口。

受管执行继续使用 collaboration_work(action=execute) 或已有的 collaboration_work_execute。新旧工具共用原幂等记录、attempt 和 fencing token；不得用一般 exec、直接请求服务端 RPC、换身份或新凭据替代。

## 逐层验收

- 服务发现：MCP 工具和事件 schema 与实际安装目录一致
- 原生订阅：精确过滤器、challenge、到期和撤销有效
- 投递：2xx 仅证明回调接受，不证明模型读到了事件或执行了工作
- 读取：宿主用保存的基线，实际读取可信房主原消息与当前工作
- 范围：文字提到 VPS 不会把 project_agent 的旧允许范围变成 VPS 权限
- 领取：只有明确同意处理模式之后、当前可领取的工作使用短租约和 fence
- 执行：每步返回持久 operation_id；pending/unknown 查询原操作，不重复提交
- 完成：真实终态之后提交 work_result，结果恰好一次回复原话题

隔离测试覆盖旧目录、无事件 payload、两条任务、分页/重启、重复请求、历史不回放、仅通知、签名/模式/策略篡改、撤权、精确项目候选、真实本地 Agent 文件读取和原话题结果。测试不会创建生产订阅或执行真实 VPS 命令；生产闭环仍需在用户批准的具体范围内验收。

规范依据：[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)，特别是工具变化后的重新扫描、2xx 之后核对预期 event data，以及重复投递、撤权和反馈循环测试。

## 缺少委托 ID 时的诊断

先使用已保存的 inbox_request（旧目录使用 legacy_inbox_request）；按返回的每项 read_request 核验真实委托。不要重复派发，也不要用一般执行工具代替受管链路。

尚未完成接入时，在精确项目和环境读取 delegation_policies：delegation_recovery 会区分 COLLABORATION_ROOM_REQUIRED（当前连接可见范围没有房间）、DELEGATION_POLICY_REQUIRED（此聊天室没有当前连接可见的规则）和 SAVED_INBOX_REQUIRED（应恢复已保存的收件箱）。这些状态不证明其他账号或连接也没有配置，读取本身不创建房间、不授权、不领取。

发布源码不能替代宿主重新扫描、保存消费者配置或房主配置真实规则。丢失基线必须重新建立报告基线；已有任务不能因此自动执行。
