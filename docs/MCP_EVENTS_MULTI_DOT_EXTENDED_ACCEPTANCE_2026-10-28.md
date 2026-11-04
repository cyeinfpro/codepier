# MCP Events 与多 Dot：扩展验收和问题闭环

> 历史记录的公开脱敏版；不代表当前部署或授权状态。具体实例、内部回执与路径已移除。

日期：2026-10-28（Asia/Shanghai）
项目：MCP
基线：`[已脱敏标识]`
本轮代码变更集指纹：`[已脱敏标识]`（68 个代码文件；基线与排序后的路径、SHA-256、mode；排除 docs，避免报告自引用）。

本文延续 [首轮设计](MCP_EVENTS_MULTI_DOT_DESIGN_2026-10-28.md) 和 [首轮实施记录](MCP_EVENTS_MULTI_DOT_IMPLEMENTATION_2026-10-28.md)。首轮 5960 项 fast 结果对应首轮候选；本轮没有把它写成对当前候选重新执行的完整回归。历史运行环境核查方法另见 [运行环境就绪审查](MCP_EVENTS_MULTI_DOT_LIVE_READINESS_2026-10-28.md)。

## 1. 本轮发现并解决的问题

| 问题 | 根因及实际证据 | 已落实方案 |
| --- | --- | --- |
| 旧版本事件循环遇到新 Inbox 事件后异常 | 真实旧 Hub 的 dispatcher 在准入检查前索引旧 FILTERS，触发 `KeyError(codepier.collaboration.inbox_dirty.v2)`；失败操作 `[已脱敏标识]` | 回滚准备独立扫描全部本次 v2 原生订阅，不依赖 active binding；活动路由按精确行版本冻结为旧版支持的 revoked。过期、撤销、孤立记录也纳入范围复核；新增路由使旧计划失效 |
| 正式源码 ZIP 中没有 Git，原回滚 CLI 无法校验来源 | 原 source_stamp 依赖 git rev-parse；嵌套在其他工作树的 archive 还可能借用父 Git 身份 | 在审核过的 Git checkout 导出来源清单。无 Git 时必须提供外部已知清单 SHA，再严格核验固定 8 个源文件、脚本自身、revision 与 fingerprint；Git 模式核对实际仓库根 |
| 点击“加载更早目标”后没有结果 | 历史页等待期间后台刷新增加 epoch，旧页被正确拒收，但用户展开意图一起丢失；真实 Chromium 与独立契约先失败 | 等待当前列表刷新，使用当前有效游标有界重取一次；连续冲突给出可重试状态。旧 epoch、旧项目及旧权限范围的数据仍不能合并 |
| 展开 80 项后刷新，再点仍停在 80 项 | 保留了历史项目，却把续页游标重置到首屏，下一页全是重复数据；100 项反例明确出现 80 != 100 | 按权限与项目范围保存分页进度、已见游标与耗尽状态；同范围后台仅读首屏，不回退续页位置。范围改变时清空，验证 40→80→刷新→100 |
| 压测可能把后台曾经失败、后来恢复算作“无错误” | 生产生命周期会记录 warning 后继续，健康状态最终可能恢复或被 stop 覆盖 | 验收器增加有界、脱敏的生命周期异常计数；实际循环先抛错再恢复的反例会使验收失败，保留生产自恢复机制 |
| 压测可能只看数据库快照而忽略异步尾部 | HTTP handler、回调或消费者仍可能在运行 | 结束前同时检查消费者、回调、hint 队列、HTTP handlers 归零，并按持久 source、reply、Inbox 和投递记录核对 |
| 运行版本和接入授权需要分别核验 | 历史检查发现运行代码与候选不一致，且接入可能失效 | 使用当前回执核对版本及授权，不重放旧连接或扩大授权 |

保留了失败试跑与修复后证据；没有删除原断言或把错误改成成功。

## 2. 当前冻结候选的验收结果

| 验收 | 最终结果 | 原操作编号 |
| --- | --- | --- |
| 回滚与来源清单完整模块，外加显式历史版本验收 | 42 passed / 11.84s：41 项普通回归 + 1 项历史兼容 gate | `[已脱敏标识]` |
| 界面完整相关单元测试 | 18 passed | `[已脱敏标识]` |
| 既有浏览器回归与新增移动场景 | 20 passed / 36.32s：既有 12 项 + 新增 8 项 | `[已脱敏标识]` |
| 真实 Hub/Agent 进程中断与恢复 | 2 passed / 41.66s | `[已脱敏标识]` |
| 持续压力验收器的短测试 | 10 passed / 3.73s | `[已脱敏标识]` |
| 900 秒持续压力 | passed；900.002s，2160 输入 / 2160 唯一回复，全部结束检查通过 | `[已脱敏标识]` |
| 最终静态与资产检查 | Ruff 通过；mypy 配置范围 3 文件通过；全前端 Prettier 通过；diff 与生成资源检查通过 | `[已脱敏标识]` / `[已脱敏标识]` / `[已脱敏标识]` / `[已脱敏标识]` |

不同批次分别报告，不与首轮 5960 fast 简单相加。没有声明完整 Linux/macOS CI、Windows、全部浏览器矩阵或所有进程故障路径通过。

## 3. 持续压力：实际负载与结果

运行命令：

```sh
PYTHONPATH=. /path/to/python scripts/soak_multidot.py \
  --output .work/multidot-soak-900s-20261028-a
```

默认参数为 900 秒真实时间，3 个 owner、3 个独立 Space、每个 owner 4 个 Dot；每 5 秒提交一批 12 条输入。使用生产服务、生命周期循环、SQLite WAL 和签名回调，经明确的测试适配器发送到真实回环 HTTP 接收器。一个端点较慢，另一个端点每 180 秒注入 8 秒 503。保留原请求门禁、投递次数与字节预算以及原重试等待，不修改时间来缩短故障恢复。

消费者是确定性测试客户端，通过公共动作回复原输入并排空；不是实际 Dot 模型。没有执行用户真实工作或收费模型调用。

| 指标 | 本次结果 |
| --- | --- |
| 测量 / 末尾排空 | 900.002s / 0.175s |
| 输入与唯一绑定回复 | 2160 / 2160；3 个 owner 各 720 / 720 |
| 稳定原生订阅 | 12 条；没有按输入或 Goal 增加订阅 |
| 持久提示结果 | 2141 accepted；19 条 mailbox_coalesced；全部原来源保留 |
| 503 故障 / 未预期错误 / 生命周期 warning | 4 / 0 / 0 |
| 结束时未处理输入、dirty binding、活跃 session、execution operation | 全部 0 |
| 消费者、回调、hint 队列、HTTP handler / Store queued、running | 全部 0 |
| 健康端点事件→回调 p50 / p95 / p99 | 237.955 / 596.581 / 687.704ms；1800 个样本 |
| 健康输入→fixture 回复 p95 | 973.800ms；不是模型响应时间 |
| 故障端点事件→回调 p99 | 30206.364ms；保留生产退避 |
| 回调并发峰 | 全局 4；单端点 1；单 owner 不超过 2 |
| RSS 观测 | 94.609→101.625MiB；峰增长 7.016MiB，61 次采样 |
| Store 末段窗口 p95 | queue 94.823ms；phase 22.7ms；最外层事务 23.016ms |

每个故障窗及随后 5 秒的观察区间，其他两个 owner 各完成 12 次回调。原重试正文摘要保持不变，SQLite integrity=ok。

实际出现 1 次预期 DOT_INBOX_CHANGED 竞态，按最新 revision 安全重读后完成；它对应测量期唯一的 worker failed/事务 rollback，不是提交或回滚失败。commit_failed 与 rollback_failed 均为 0。Store 的分位数只取最后 2048 个近期样本，已越过 setup；不是整个 900 秒的分位数。预算检查覆盖已观测的一秒窗口及持久余额，不声称完成任意时间区间的 token bucket 形式证明。

正式结果：`[本地验收目录]`，SHA-256 `[已脱敏标识]`；读回操作 `[已脱敏标识]`，持久台账复核 `[已脱敏标识]`。Hub/shared/runner 前后指纹均为 `[已脱敏标识]`；整份 68 文件代码变更集在运行前后也与本文页首一致。

健康、慢端点和故障端点分别统计。故障端点包含原生产退避产生的长尾，不能混成正常用户时延。RSS 是同一验收进程的观测，包含 harness、采样容器与 SQLite；业务持久行增长及一次 15 分钟样本不能证明不存在长期内存泄漏。本次通过只覆盖这个明确定义的功能和负载场景，不等于生产延迟 SLO 或全部预算轨迹证明。

运行输出目录包含匿名结果与进度；没有导出 wake ticket、consumer proof、签名密钥或原始私人对话。运行前后代码摘要必须一致，异常恢复不能抹去异常计数，等待队列未排空不能报告成功。

## 4. 真实进程中断验收覆盖了什么

第一条故障链使用真实 Uvicorn Hub 子进程，实际两次 kill/restart，共三个 Hub PID。回调正文已经收到后断开 HTTP 回包，consumer_begin 回包由测试代理丢弃；重启后核对原票据、proof、lease 与原投递正文，真实等待 30 秒租约到期，验证新 fence 生效、旧消费者及跨 Dot 材料被拒。两个原输入都得到回复并排空。

第二条故障链启动真实 Hub 与 Agent。Hub 被 kill 后原 Agent PID 和实际命令继续运行；后续共享资源工作保持 waiting_resource，并引用原阻塞 operation。恢复沿用原 operation ID，两个实际命令的副作用计数分别为 1，完成事件只投递给相应 assignee。unknown 状态仅在测试 Hub 停止后明确注入，不冒充一次真实网络故障自动生成的未知状态。

这些是两条真实进程故障链；native 回调仍通过 localhost 测试适配器，不覆盖目标宿主调度、公网 DNS/TLS/SSRF 路径或任意故障组合。

## 5. 旧二进制回滚与来源校验

历史验收从本地 Git 精确基线导出源码，实际启动旧 Hub 和当前 Hub，并经回环 HTTP 验证。每个进程各检查 26 个旧别名/公共准入口被拒；这属于一个历史版本测试中的断言，不另计为 52 项测试。原 2 个 operation 可读，sealed retry 正文、attempt、fence 和受保护台账保持不变。旧 dispatcher 成功处理同 grant 的无关 v1 通知一次，当前版本没有重播。

历史验收入口需要精确 Git 基线对象，作为显式门禁运行：

```sh
PYTHONPATH=. /path/to/python -m pytest -q -p no:cacheprovider \
  --basetemp=.work/multidot-old-binary \
  tests/acceptance_multidot_old_binary.py
```

它没有混入无 Git 发布源码 ZIP 的默认 pytest 收集；不是以 skip 造绿，也未声称发布 CI 自动执行了该历史门禁。普通回滚及来源清单单测仍在 `tests/test_events_multidot_rollback.py` 默认收集中。

源码 ZIP 使用来源清单的方式：

```sh
# 在已审核、精确匹配候选的 Git checkout 导出；不读取数据库。
python scripts/prepare_multidot_rollback.py \
  --export-source-proof /private/source-proof.json

# 无 Git 的精确同源部署包，plan 与 apply 都必须带这两个参数。
# SOURCE_PROOF_SHA 来自已审核交付渠道，不是现场任意自算后宣布可信。
python scripts/prepare_multidot_rollback.py --database /private/hub.sqlite3 \
  --source-proof /private/source-proof.json \
  --expected-source-proof-sha256 SOURCE_PROOF_SHA \
  --plan-out /private/rollback-plan.json
```

上例保持默认只读；实际 apply 还须原工具要求的精确计划摘要与所有 writer 已停止确认。工具不负责停止 Hub，也不授予生产操作权限。清单匹配证明“当前源码与给定清单一致”，不证明签名、发布方身份或部署授权。

回退期间禁止使用旧版 rekey/密钥维护：旧代码不认识新增密文列及 sealed body。需要密钥维护时先恢复兼容的前向代码。禁止恢复旧数据库快照覆盖已经接收新写入的 ledger，禁止自动重新启用冻结的接入和工作。

## 6. 移动浏览器体验

使用 Chromium 153.0.8010.12 与 WebKit 26.6、移动 viewport/touch、真实临时 Hub/Agent 与原生 EventSource。覆盖：

- 更新到达时中文草稿、焦点、光标与选择区保留。
- 离线或 HTTP ACK 丢失后按原回执恢复，只发生一次 POST、数据库恰好一条输入。
- 后台恢复时最多一个未完成读取与一个尾随读取。
- 团队主责选择，历史页遇后台刷新有界重取，已展开范围保持续页位置。
- 320/390 宽度无横向溢出，压缩 viewport 时输入框仍可见。

100 项 Goal 历史使用明确的 HTTP UI fixture；团队确认、SSE 与草稿等使用真实临时 Hub。composition 和后台状态由 DOM 事件模拟，不能当作物理 iPhone、系统中文输入法或操作系统后台存活的实机证明。

最终证据：`[本地验收目录]`，8 份匿名 JSON、8 张 PNG 与 JUnit。两张 WebKit 截图已实际查看。首屏读取可能随实际 SSE 追加；只对有界历史续页与特定单飞场景断言精确次数，不把所有 GET 固定成一个人为数字。

## 7. 交付与未完成边界

本轮新增或更新的 11 个代码、测试和生成资源文件已按精确 SHA 写回原 MCP 目录，68 个候选代码文件逐一复核一致，原有 8 个无关修改保留。代码落地回执 `[已脱敏标识]`，清单 SHA-256 `[已脱敏标识]`。

写回后在原目录运行 smoke：20 passed，9519 deselected，9 warnings，8.67s；操作 `[已脱敏标识]`。这些 warnings 来自测试框架弃用提示，不是被忽略的失败。该批证明所选黄金路径与保留的原目录修改共存，不等于在原目录完整重跑所有测试。

设计文档和首轮记录已增加后续验收索引，新增本报告与就绪审查随相同源码交付。历史版本 gate、loopback 压测、浏览器模拟、真实进程测试和生产现场只读审查分别保留证据边界。

历史现场检查的实例和接入状态不在公开版披露；本轮测试没有执行生产部署或恢复授权。

真实端到端体验仍需：精确新候选通过正式发布门禁并上线，在当前有效房间重新建立明确接入，由实际宿主完成 v2 原生 challenge 与 consumer_begin，再验证两个真实 Dot 的同 Goal 协作。物理手机验收、完整跨平台发布 CI、长期容量规划及更多故障组合继续单独报告，不能由本轮自动化替代。
