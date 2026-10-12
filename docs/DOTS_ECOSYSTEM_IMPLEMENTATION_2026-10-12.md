# Dots 生态设计实施与验收对照

状态：本侧功能实现与隔离回归完成；三个WebKit宽度的详情切换性能目标未达标，真实宿主/账号条件项仍未验证。不是全指标达标或已上线证明。

## 基线与范围

对应设计 `DOTS_ECOSYSTEM_ADOPTION_DESIGN_2026-10-12.md` 输入原稿 SHA-256：`29b41e75747d7f9b6816a467a08dbc236e86d328dd2f493511f8ffc07382c830`。仓库副本仅将头部三行的空格硬换行规范化为 Markdown 反斜杠硬换行，保持内容和换行语义；副本 SHA-256：`35ce82a4aa3c979960c689ae22ef321cc339129ae66e55ac9bbb6bea8252e9a1`。

实现由 `0fd57e9694b847c30572590294e5e708909e6d90` 开始，最终集成基点为 `f2295144c89a61a0e8131f60fb887c82e4dd24e0`，保留 Windows 后续提交、`f43d487` fluid UX 合并及全站 UI；资源已统一重建。此文和代码由同一后续提交交付，验收数不拼接为旧树证明。本轮复用原 Hub/Agent、消息/工作/外发账本；没有引入第三方源码或新依赖，没有改变真实凭据、权限、订阅期限或生产配置。设计内的第三方说明不构成运行授权。

## 逐章落地

| 设计项 | 实现与代码 | 验证/边界 |
|---|---|---|
| 1–3 目标、参考、采用判断 | 采用逐输入处理、轻量阶段反馈、能力交集、局部恢复；不引入外置插件平台或超时重跑 | 本地实现，无第三方源码/新依赖 |
| 4 已有能力与增量 | 复用双向聊天、接入预授权、事件、原任务恢复；新增投影/批查询/处理关系/媒体清单 | 旧消费者和授权继续兼容 |
| 5 身份与不变量 | 原键、原Dot/项目/线程、当前授权、未知副作用不重放；查询不写入 | message_handling、dot_relay及既有scope守卫 |
| 6.1 原接入继续 | connection投影与collaboration-dots.js导航到原slot | 不新建grant/绑定，不伪造就绪 |
| 6.2 集中说明 | channel_enrollment复用冻结摘要和分目的consent；检测范围差异 | 凭据保存/条款/本人/未来任务证据不合并 |
| 6.3–6.8 日常聊天与补充 | dot_chat、dot_update、dot_handle；普通聊天不建work，补充保留来源，执行对象唯一 | 不宣称补充能抢占模型/命令 |
| 7 主状态/下一步 | experience_projection.py分离收件、处理、执行、验收、发表和外发；受控导航 | 泛授权等待只指向原解释；未知不假完成 |
| 8 处理与覆盖 | message_handling.py加法表、schema version、CAS、最多20条原子处理；直引同Dot原线程最终回复 | 旧handled标legacy；不建立执行租约，不证明语义质量 |
| 9 阶段与审批 | relay原位阶段卡、最终态保护、稳定等待标识 | 没有真实宿主定位证据时不构造审批深链 |
| 10 能力与媒体 | channel_capabilities.py四层交集；channel_file_review.py；成果清单分离上传/发送/分享 | 文件/当前目标/版本变化重新审阅；unknown局部隔离 |
| 11.1 链路时间 | 复用dot_wake_context实际事件、显式收件、原操作、发表时刻 | 不把回调接受当模型已读；跨机未知保留未知 |
| 11.2 批查询 | receipts_many上限124、请求内权限复用、集合批查；影子对照不一致回退 | SQL及1000成对样本；权限不由前端过滤 |
| 11.3 渲染 | revision/dirty IDs、无变化不重建大DOM、保留输入法/选区/锚点 | 同版本但证据变化仍需使回执失效 |
| 11.4 上下文 | 既有dot_wake_context有界只读包、总字节预算、各集合分页 | 权限过滤空页仍保留下一页；不ack/claim |
| 11.5–11.7 并发/成本 | 保留资源锁、项目公平、控制余量、通道冷却 | 不据单个semaphore提高并发；SQL不等于host提速或token账单 |
| 12 恢复 | 原journal、原键回执先行、暂停/撤权/永久拒绝不自动恢复 | 持久宿主outbox仅参考适配器，真实安装未验证 |
| 12.6 本机待发P2 | 默认关闭，显式开启，仅已点击发送内容；20条/256KiB UTF-8/24h；当前账号/空间/话题/服务端范围版本 | 恢复先查原键；GET和POST事务都核对revision；退出/撤权/变更清除 |
| 13 UI/动效 | 一个工作区、真实状态、reduced-motion、键盘与窄屏、稳定表单 | cc-*局部样式与全站token并行整合 |
| 14 数据/接口 | 加法数据、可选字段、原事务、旧协议兼容；原事实不可由缓存替代 | 契约、回滚、幂等及权限测试 |
| 15 诊断 | 五层只读、固定白名单、匿名引用、预览后复制；HTTP no-store | 不导出正文、路径、联系人、凭据或跨项目记录 |
| 16 P0/P1 | 以上实际代码与回归 | 不是仅文档TODO |
| 16 P2条件项 | 本机待发实际实现；长期订阅本地模型/降级；编辑/新通道声明不支持 | 模型不等于真实长期订阅；能力后选用，不无限扩大范围 |
| 17 灰度/回滚 | 新投影/批查询/影子开关，历史保守迁移；旧授权不扩大 | UI回滚不恢复旧DB快照，不删除副作用事实 |
| 18 验收 | 契约/隔离Hub/双浏览器/性能分层 | 模拟不证明真人收到或Dot自主消费 |
| 19 ADR/风险 | 保留单事实源、读写边界、原工作不盲重跑、宿主未知、项目范围非OS沙箱 | 无第三方许可变化 |
| 20 完成定义 | 下表逐场景定位与最终同提交证据 | 组件存在、截图漂亮、旧CI均不足证明完成 |

## 验收场景矩阵

下列是代码断言入口；测试文件存在不代表执行通过。实际执行见后节，真实宿主/外部账号统一未验证。

| 编号 | 必须保留的断言 | tests/覆盖入口 |
|---|---|---|
| C01 | 普通回复无多余work | test_collaboration_dot_chat.py |
| C02 | 阶段回复不终结输入 | test_collaboration_dot_chat.py、test_collaboration_message_handling.py |
| C03 | 三输入原子直引同一最终回复 | test_collaboration_message_handling.py：same_thread_batch_coverage |
| C04 | 旧输入/回复/范围版本拒绝 | 同上：stale_source_reply_and_scope |
| C05 | 同文两次是不同意图，重传同键去重 | 同上：same_text；test_collaboration_dot_relay_flow.py |
| C06 | 无需回复显式申报，无关键词丢弃 | 同上：no_response_is_explicit |
| C07 | 等待资料保留原线程和输入 | 同上：waiting_requires_real_interim；test_dot_wake_context.py |
| C08 | 原工作中补充可读，不假抢占 | test_dot_wake_context.py：duplicate_wake_reuses_interim |
| C09 | 多Dot唯一执行对象守卫 | test_collaboration_delegation_consumer.py：multi_target_send_requires_choice |
| C10 | 最终版本不被旧更新覆盖 | test_collaboration_dot_relay.py、test_dots_ecosystem_ui_browser.py |
| A01 | 原待接入对象继续，不重建 | test_channel_capabilities.py：enrollment_readiness；test_channel_enrollment_browser.py |
| A02 | 原预授权及本人码原子完成，目的独立 | test_channel_enrollment_flow.py：preapproved_code、every_preapproval_purpose |
| A03 | 摘要变化拒绝新范围 | 同上：changed_scope_or_expired_code |
| A04 | 错联系人/凭据世代拒绝继承 | 同上：explicit_expected_contact、changed_scope_or_expired_code |
| A05 | 旧消息重投不成新任务 | 同上：preapproval_message_redelivered |
| A06 | timed不自动长期 | test_dot_weixin_owner_authority.py、test_dot_telegram_channel.py |
| A07 | 暂停/撤权不自动恢复 | test_collaboration_dot_relay.py、test_channel_enrollment_flow.py |
| A08 | 当前只读不能发送，旧响应丢弃 | test_collaboration_dot_relay_flow.py、test_dots_ecosystem_ui_browser.py |
| A09 | 附件/引用/回声不成本人指令 | test_dot_weixin_owner_authority.py、test_dot_telegram_channel.py |
| R01 | 已提交丢ACK关联原记录 | test_dot_durable_outbox.py、test_collaboration_dot_relay_flow.py |
| R02 | unknown/普通404不视为未提交 | test_dots_ecosystem_ui_browser.py：unknown_send_receipt；test_dot_experience_http.py |
| R03 | 同键异文/身份明确冲突 | test_dot_durable_outbox.py、test_collaboration_dot_relay.py、test_collaboration_message_handling.py |
| R04 | 查询total_changes不变 | test_dot_experience_projection.py、test_collaboration_message_handling.py、test_dot_wake_context.py |
| R05 | 过期fence/未知操作不能重跑 | test_dot_task_liveness_recovery.py |
| R06 | 只恢复结果发表，操作集合不变 | test_dot_experience_projection.py：terminal_original_operations；test_dot_task_liveness_recovery.py |
| R07 | 必要失败/未知不假验收 | test_dot_task_liveness_recovery.py；handling的unfinished_or_failed_work |
| R08 | 重启计数/游标有界 | test_collaboration_dot_relay.py、test_collaboration_events.py |
| R09 | 乱序/重复/永久拒绝 | test_collaboration_dot_relay.py、test_collaboration_events.py、test_mcp_events_protocol_compat.py |
| R10 | 通道局部异常不重启其他端 | test_dot_telegram_channel.py、test_delivery_concurrency.py |
| D01 | 原结果保留，单端unknown独立 | test_dot_experience_projection.py：channel_unknown；test_dots_ecosystem_ui_browser.py |
| D02 | accepted/429独立冷却 | test_dot_telegram_channel.py |
| D03 | 仅合并未发阶段，不删历史 | test_dot_weixin_channel.py |
| D04 | source-reply与mirror单所有者 | test_dot_weixin_channel.py |
| D05 | 镜像回声不循环造任务 | test_dot_telegram_channel.py、test_dot_weixin_owner_authority.py |
| M01 | 上传不是送达，超时不重发 | test_channel_capabilities.py：manifest_separates_upload_from_send；媒体回归 |
| M02 | 文件/目标/绑定版本改变失效 | 同上：changed_file_review、ready_cache；test_dot_channel_shares.py |
| M03 | 正文镜像不自动外发二进制 | test_dot_channel_shares.py、test_channel_capabilities.py |
| M04 | 缓存/分块有界保留进度 | test_dot_telegram_channel.py：quota_*；test_dot_weixin_extended_acceptance.py：offline_artifact_resumes_original_chunk_offset |
| M05 | 中文/emoji/组合字符正确分段 | test_dot_weixin_channel.py、test_dot_telegram_channel.py |
| M06 | 无转写不伪造指令 | test_channel_capabilities.py、test_dot_weixin_owner_authority.py |
| U01 | 输入法/同一输入框/选区 | test_dots_ecosystem_ui_browser.py、test_collaboration_workspace_flow.py |
| U02 | 历史滚动锚点保留 | test_collaboration_dot_relay_flow.py、test_collaboration_workspace_flow.py |
| U03 | 旧空间/账号响应丢弃 | test_dots_ecosystem_ui_browser.py、test_collaboration_dot_relay_flow.py |
| U04 | 示例仅填空草稿，不提交 | test_collaboration_workspace_flow.py |
| U05 | 键盘/窄屏/返回原消息/无溢出 | test_collaboration_workspace_flow.py、真实五宽度浏览器验收 |
| U06 | reduced-motion无伪动画 | test_dots_ecosystem_ui_browser.py |
| U07 | 诊断无正文/路径/凭据 | test_dot_experience_projection.py、test_dot_experience_http.py、test_dots_ecosystem_ui_browser.py |
| P01 | 新旧语义/权限一致，SQL实测 | test_dot_experience_projection.py：batch_receipts、fixed_receipt_performance_samples |
| P02 | 无变化0大DOM重建 | test_dots_ecosystem_ui_browser.py：unchanged_sync |
| P03 | 公平/控制余量/撤权不重试 | test_adaptive_project_scheduler.py、test_gateway_concurrency.py、test_scheduler_queue_admission.py |
| P04 | 空页仍有独立分页和预算 | test_dot_wake_context.py：work_and_recovery_pagination、context_budget |
| P05 | 页面退出清理监听/计时器/缓存 | test_collaboration_workspace_flow.py、test_dots_ecosystem_ui_browser.py |

## 灰度与回滚

- `CODEPIER_EXPERIENCE_PROJECTION_ENABLED=false` 关闭新投影；原事实/回执保留。
- `CODEPIER_RELAY_BATCH_ENABLED=false` 使用旧逐条路径。
- `CODEPIER_RELAY_BATCH_SHADOW=true` 新旧对照，不一致保守退回旧结果并返回脱敏比较状态。
- 未改变生产环境值。前端布尔值不能跳过后端授权；旧授权不自动扩大。
- UI回滚不能恢复旧DB快照或删除处理/投递关系；未知外发不得重放。
- 本机保存独立显式启用，关闭清理；回执与提交事务都核对当前服务端范围版本。

## 实际执行记录

以下使用整合后的真实源码；不同批次的重叠测试不累加为全仓测试数：

- 受影响后端完整模块回归：1386 passed，422 deselected，1个既有Starlette弃用warning，159.71秒。排除了browser与slow，不称全仓/全部集成通过。
- 独立官方 MCP SDK：2 passed，5.62秒；实际回环 HTTP，legacy `2025-11-25` 与 auto `2026-07-28`，包括错误响应schema核对。
- 回执基准和1/24/100×1/3收件人SQL对照：9 passed；每条性能路径1000测量，查询前后业务写入数不变。
- 1/5/20独立客户端HTTP持久化：3 passed，7.43秒。首次新增基准错误假设fixture操作总数为0，现改为保留项目预检原operation ID集合，不删除操作或弱化无新增执行断言。
- 通道能力/接入/导航/任务存活浏览器完整模块：100 passed，106.98秒；最终源码的全站20路由/主题/键盘/窄屏矩阵：19 passed，19.40秒。
- 独立运行的双浏览器密度/配色/窄屏模块：40 passed，63.89秒。该模块自持Playwright生命周期，不能与session复用pool混跑；混跑的36个生命周期错误已通过分进程重跑核实。
- 协作/流畅UI/聊天/恢复完整浏览器模块：67 passed，160.31秒。已修复选区释放丢补刷新及@选择后示例按钮残留禁用；保留原行为断言，新增两引擎先失败后通过的回归。旧收件文案断言随统一投影更新，另加强唯一主状态、原接收属性兼容和数据库未创建任务断言。
- 真实HTTP/P2与五宽度长列表：4 passed，501.58秒。这是功能断言通过，不代表下列所有性能门槛通过。丢ACK只保存一次；恢复先GET原键，同键最终仅1条；服务端scope变化POST409且0新提交、清理本机记录。原工作没有重跑。
- 已目视核对实际1440px Chromium和390px WebKit的P2入口/开启状态，以及1000历史离线视图截图；控件、输入草稿和说明可见，无横向溢出。截图全部为合成fixture，未导出真实用户数据。
- Ruff正确性、前端build/manifest一致性、公开源检查通过；凭据模式命中0不等同完整无敏感信息保证。
- 使用既有依赖，未安装软件。Node静态检查此前被执行策略阻止且未启动，未绕过，不写为通过。

### 可复现的验收入口

使用项目已有 Python、浏览器及官方SDK环境；真实Hub的解释器由 `CODEPIER_TEST_STACK_PYTHON` 指向已有完整环境。没有新增依赖。

- 后端：`python -m pytest -q -m 'not browser and not slow' tests/test_collaboration*.py tests/test_dot*.py tests/test_channel*.py tests/test_subscription_lifetime.py tests/test_adaptive_project_scheduler.py tests/test_gateway_concurrency.py tests/test_delivery_concurrency.py tests/test_scheduler_queue_admission.py tests/test_mcp_events_protocol_compat.py tests/test_operation_completion_events.py tests/test_public_collaboration_contracts.py`
- SDK：设置 `MCP_COMPAT_PYTHON` 后运行 `python -m pytest -q tests/test_v140_compatibility.py`。
- 协作：`python -m pytest -q tests/test_dots_ecosystem_ui_browser.py tests/test_collaboration_fluid_cache.py tests/test_collaboration_fluid_ui.py tests/test_collaboration_dot_relay_flow.py tests/test_collaboration_workspace_flow.py tests/test_collaboration_dot_chat_flow.py tests/test_collaboration_dot_seamless_flow.py --chat-browser-reuse`
- 通道：`python -m pytest -q tests/test_channel_capabilities_browser.py tests/test_channel_enrollment_browser.py tests/test_dot_channels_browser.py tests/test_dot_channel_navigation_browser.py tests/test_dot_task_liveness_browser.py`
- 全站：`python -m pytest -q tests/test_site_experience_browser.py --chat-browser-reuse`；另起进程运行 `python -m pytest -q tests/test_ui_comfort.py`。
- 真实HTTP与长列表：`python -m pytest -q -s tests/test_dots_ecosystem_integrated_browser.py --chat-browser-reuse`；可设置 `CODEPIER_ECOSYSTEM_EVIDENCE` 保存合成截图及JSON。
- 基准：`python -m pytest -q -s tests/test_dots_ecosystem_http_benchmark.py tests/test_dot_experience_projection.py -k 'bounded_authenticated or fixed_receipt_performance or batch_receipts'`；可设置 `CODEPIER_EXPERIENCE_BENCHMARK_REPORT` 保存JSON。
- 构建/公开检查：`python scripts/build_web_assets.py --check`、`python scripts/check_release.py`、Ruff正确性检查与 `git diff --check`。

## 最终后端源码的本地回执性能分解

环境：macOS 26.3.1 arm64、Python 3.13.12、隔离SQLite、无网络。固定已保存但无活动订阅的合成消息；每条路径预热5次，1/24/100条各1000次。当前SQL数包含新处理/投影，不能沿用更早只有4条查询的候选数字。全部调用只读、错误率0。此处不含公网、真实host、活动订阅和大量真实工作权限检查，也不代表生产SLA。

| 消息数/收件人 | 旧SQL | 完整投影批查SQL |
|---|---:|---:|
| 1/1 | 3 | 6 |
| 24/1 | 72 | 6 |
| 100/1 | 300 | 6 |
| 1/3 | 7 | 6 |
| 24/3 | 168 | 6 |
| 100/3 | 700 | 6 |

| 消息数 | 旧路径p95 ms | 同旧字段批查p95 ms | 完整新投影p95 ms |
|---|---:|---:|---:|
| 1 | 0.0165 | 0.0240 | 0.0614 |
| 24 | 0.4073 | 0.1414 | 0.7065 |
| 100 | 2.4861 | 0.5517 | 2.9057 |

相同事实的批读取在24/100条样本减少开销；完整新投影增加处理、当前可见性核对、多个证据维度和稳定revision序列化，其总耗时高于旧简化回执。100条完整结果约115500字节、旧结果约19300字节，返回形状不同也影响字节数，不能解释成相同载荷提速。更早完整投影一次样本p95为24条0.9040ms、100条3.3190ms，与本轮一致显示功能成本；未隐藏这项退化。上表已在最终后端源码复测；后续前端兼容修复不改变此路径。SQL降低不等同模型响应提速。

## 隔离 HTTP 持久化与并发样本

`test_dots_ecosystem_http_benchmark.py` 使用真实回环 Hub/Agent、认证、授权和事务，同一已授权房主/项目的独立池化客户端。短文本、长中英文、代码块、表格轮换。保留原每作者/项目每分钟60条保护，每个新fixture 5次预热、50次测量；不是设计建议的1000样本长稳态，也不代表多项目公平、真实宿主或公网。50条意图都唯一落库，没有新增work，原项目预检operation ID集合保持不变。

| 并发客户端 | 样本 | p50 ms | p95 ms | 最大 ms | Hub RSS变化 KiB | 错误率 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 50 | 2.27 | 3.08 | 4.04 | 192 | 0 |
| 5 | 50 | 10.66 | 12.35 | 13.11 | 416 | 0 |
| 20 | 50 | 38.45 | 45.63 | 47.88 | 1280 | 0 |

本样本低于300ms提议门槛，但样本较粗，不宣称生产SLA或内存上限。RSS是隔离Hub进程前后读数，不是峰值或完整泄漏分析；没有关闭限流、删除授权检查或扩大生产范围。

## 最终双浏览器长列表结果与未达标项

固定同一最终源码和macOS测试机。Chromium `153.0.8010.12`、WebKit `26.6`；实际面板和生产处理函数，先加载连接详情，再对1条真实消息派生的1000条合成已加载DOM进行测量。每个引擎5个宽度、每宽度200次，共每指标1000次；25次一组让出事件循环。强制布局验证DOM可见/可交互，**不是compositor paint或公网时延**。reduced-motion模式，离线本地发送；不声称服务端1000条分页或真实宿主已验证。性能阶段未并行运行其他本轮测试。

| 宽度 px | Chromium气泡 p95 ms | Chromium详情 p95 ms | WebKit气泡 p95 ms | WebKit详情 p95 ms |
|---|---:|---:|---:|---:|
| 390 | 9.6 | 16.7 | 6 | **107，未达100ms目标** |
| 768 | 9.5 | 16.4 | 7 | **107，未达100ms目标** |
| 1024 | 8.7 | 15.5 | 7 | **107，未达100ms目标** |
| 1440 | 8.4 | 31.4 | 6 | 81 |
| 1920 | 8.5 | 25.8 | 6 | 79 |

两个引擎都满足无变化0条正文重渲染、原DOM节点保持；更新第500条只渲染该条。五宽度输入框/中文组合选区/滚动容器身份保持，无横向溢出，减少动态效果时无强制动画。

**已知性能缺口不能省略：** §18.3的详情切换100ms提议目标在WebKit三个窄宽度均未达标。保留1000条负载和完整辅助技术/授权行为，没有缩小样本、删保护或把功能通过改写成性能通过。先前候选147/104/103ms只作历史，以上107/107/107ms才是最终值。该设备/引擎下的1000已加载DOM详情切换不应宣传稳定低于100ms；后续优化需保留选区、焦点、inert和滚动契约后重新采样。非P2宿主条件项，而是本侧明确的性能限制。

## 历史草稿与默认目录对账

默认旧工作目录的未提交 `liveness.py` 草稿没有覆盖到本次候选。其 operation观察时间、输出时间、work heartbeat/progress、回复持久回执和原结果恢复已由当前 `task_liveness.py` 及调用链实现；当前聚合额外执行调用者与work权限核对。未发布草稿的 `dot_reconcile`／`dot_activity` 名称不作为已发布兼容协议；当前公开入口是 `dot_receipt`、`dot_recovery`、`work_evidence`，普通聊天收件/阶段/处理沿原ack/update和显式dot_handle。

旧 `DOT_DUPLEX_RECOVERY_AND_TASK_LIVENESS_DESIGN_2026-10-11.md`（SHA-256 `d2653fb0d95924e61f6b7fea898ebb7a0298dc88f6c2fc8c736939f1c0017c9d`）原件保留本地，作为历史未发布草稿，不进入新版公开运行包。本轮矩阵逐项针对新DOTS设计，不将旧稿全部需求签发为已完成，也不声称所有默认目录文件都已发布。与两稿重叠的真实宿主持久队列、原生审批、自主消费、长期订阅、实际桌面和外部实收边界如下。

## 必选实现、条件项与真实验收的区别

- **A：本侧必选实现。** 新只读投影、原对象继续、处理/覆盖、阶段卡、通道能力/成果清单、有界上下文、诊断、批查与局部渲染均有代码和场景断言。浏览器持久待发虽然在§12.6标P2，本轮也完成默认关闭的可选实现和真实HTTP跨刷新验证。最终UI性能结果单列，不因功能测试通过而宣布达到时间门槛。
- **B：原文条件化后续。** §12.2明确“P0 保留当前有效期和续订策略”，§16.6要求长期订阅等“分别立项验证”，缺能力则降级；实际无期限订阅运行时仍未实现/未启用。§12.3称宿主outbox为“可选参考适配器”，不能单方证明真实宿主已经安装。§9.4仅“已有真实审批请求引用”才使用宿主安全链接，无引用按原说明降级。不能用本地模型、普通入口或合成宿主替代这些证据。
- **C：代码已具备，但真实层未验。** §11.1有分层时间证据，§18.5还要求原Dot收到/回复、连续补充、断连恢复、每端实收、文件分享和撤权后停止。需要指定并批准原连接、具体测试收件人、合成文本/文件和撤权对象；发布动作的许可本身不等于这些外发或权限变更许可。§13.5的桌面键盘、指定宽度已双引擎覆盖，真实设备软键盘/安全区仍需可用的指定设备验证，不能把浏览器仿真说成已测试用户桌面。

原文§18.5要求未完成的验收层在发布说明/能力描述中“明确列为未验证”；§19.4明确上述宿主未知“不阻止P0”。因此本侧实现完成与真实平台全能力完成是不同结论；本报告保留后者的具体缺口，不将必选项因困难改称可选。

## 条件项与未验证

| 条件项 | 当前实现/降级 | 缺少证据 |
|---|---|---|
| 用户桌面与原生宿主实机 | 隔离Hub/Agent与既有Chromium/WebKit自动化 | 实际用户桌面/会话验证；浏览器引擎通过不等同本机桌面全流程 |
| 原生Dot自主消费和时延 | 分层事件/显式收件/原操作，未知保留未知 | 获准的真实宿主原账号测试 |
| 原生审批精确导航 | 原说明/宿主入口，无虚构深链 | 宿主可信原审批定位能力 |
| 宿主持久outbox安装/重启 | 可选shared/dot_outbox参考适配器 | 实际宿主持久存储、安装与重启证据 |
| 长期原生订阅 | 本地模型及24h有限降级；真实无期限运行时未实现/未启用 | host/account能力、独立同范围批准、真实撤权/重启恢复 |
| 原生编辑/新通道/语音转写 | 能力矩阵不支持或未知，文本/下载降级 | 具体选用需求与平台/账号能力验证 |
| 外部文件/消息真实收到 | 分项回执与mock错误分类 | 获准具体收件人/文件实测；接受仍非已读 |
| 公网及生产高并发性能 | 本地固定样本与公平调度回归 | 实际设备/网络/负载观测；SQL不代表host提速 |
| 最终CI和发布 | 本实现阶段提交安全fix分支，统一版本由独立发布流程承接 | 最终同commit CI及统一发布/部署验收；本报告不是已上线证明 |
