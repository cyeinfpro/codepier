# CodePier (`MCP`) 第一性原理全栈深度优化审计与演进方案（2026-11-18）

> **审计基线**：版本 `v1.33.25` · 分支 `main` · HEAD `1629d925355b1f011ae1f541d339df44b893ca8f`
>
> **审阅范围**　原方案从 MCP 协议、Hub、Agent、协作与原生会话、Web 与工程五个维度审阅源码。复审在同一提交清点 1,530 个 Git 跟踪文件，解析 1,146 份 Python 文件的语法树，其中 Hub 202 份、Agent 86 份、shared 67 份。另核对 136 份 Web JS、MJS、CSS、HTML 的清单，并沿关键调用链核对实现、部署脚本和测试入口。
>
> **合并状态**　原有 OPT-01 至 OPT-38 保留编号。2026-11-18 的全仓复审把验证结果写回对应条目，新增 OPT-39 至 OPT-41，并更新实施次序。同日第二轮复审以当前工作区（HEAD 加上 OPT-01 至 OPT-41 的未提交实施）为准，从调用方可诊断性、工作区扫描边界、进程启动成本、运维韧性和工程熵五个此前未覆盖的角度新增 OPT-42 至 OPT-52，见第 7 节。实施次序见第 9 节，验证范围见第 10 节。

---

## 0. 基线排除声明与第一性原理总纲

### 0.1 已落地基线（本文不重复建议，仅审计其残余边界与下一阶演进）

截至 `v1.33.25`（2026-11-17），仓库已完成以下关键架构建设，本文严格排除“把已实现能力当作新建议”的无效重复：

1. **安全与一致性审计修复（`CODE_AUDIT_REMEDIATION_2026-11-04.md` P1–P32）**：`CheckedConnection` 外层提交与回滚保护、新结果/输出/冻结任务正文 Fernet 加密、Agent 随机 nonce 握手回显、30/365 天显式干跑保留维护、附件单层密文与 HTTP 锁外解密等已入库。
2. **自适应联合资源调度与只读池（`ADAPTIVE_SCHEDULER.md` / `QUEUE_CONTENTION_IMPLEMENTATION.md`）**：联合资源与通道准入、`strict/relaxed/read_stable` 语义、短读越过无饥饿保证、`clonefile`/`reflink=always` 源码快照初版、Hub 默认 3 连接 `mode=ro` 只读池已上线。
3. **MCP 上下文初版瘦身（`v1.33.6` / `v1.33.20`）**：二分整行预算裁剪 `_fit_prefix`、Agent `tree` 默认遵循 Git 索引与单目录限量、6 个只读动作的等待解包 `unwrap_waited`、写操作自动登记知识地图维护已入库。
4. **OAuth 连接迁移与任务预检（`v1.33.21`–`v1.33.25`）**：OAuth 统一授权期限与旧刷新令牌原样回传兼容、命名任务 `requirements`（`env`/`executables`/`files`）启动前预检、本机执行最多 8 MiB `output.redacted.log` 脱敏前缀留存已落地。

### 0.2 五大维度的第一性原理审视框架

将 CodePier 还原为最底层的物理与信息论约束，一个远程开发控制与多智能体协作底座的极限效率取决于五个正交方程：

1. **大模型上下文与协议层（信息熵与往返延迟）**
   $$\text{端到端研发耗时} = \sum_{i=1}^{R} \left(\text{RTT}_i + T_{\text{TTFT}} + \frac{\text{Tokens}_{\text{out},i}}{v_{\text{gen}}}\right), \quad \eta_{\text{ctx}} = \frac{\text{模型决策新增事实字节}}{\text{JSON-RPC 总传输字节}}$$
   任何被默认参数锁死的紧凑分支、双份序列化的正文、迫使“搜完必读”的缺失上下文行、迫使“整文件覆写”的批补丁缺口，都会按调用次数乘数级放大模型延迟与昂贵的 Output Token 成本（输出单价约为缓存输入的 50 倍）。
2. **Hub 存储与并发热路径（阿姆达尔定律与串行临界区）**
   $$Q_{\max} \le \frac{1}{T_{\text{crit}}(\text{SQLite Writer})}$$
   在单写线程（`StoreWorker`）+ WAL 只读池架构下，写锁持有时间 $T_{\text{crit}}$ 决定了全实例吞吐上限。将无状态密码学（Fernet 加解密）、大 JSON 编解码塞入 `BEGIN IMMEDIATE` 事务，或用连接级粗粒度 `PRAGMA data_version` 校验只读快照，会人为制造写锁膨胀与只读伪失效风暴。
3. **Agent 本机执行与磁盘 I/O（OS 进程孵化下限与持久化偏序屏障）**
   单次 SSD 读取仅需 `0.1~0.25 ms`，而冷启动 Python 解释器固定消耗 `15~65 ms`；崩溃可恢复性的充要条件是**偏序刷盘屏障（Barrier Ordering）**而非逐文件重复备份与逐 Token `FULL fsync`。
4. **协作中心与原生会话（状态机最小充分物化与事件边沿驱动）**
   同一进程或受控管道内的生产者—消费者应由提交钩子（`after_commit`）或文件描述符边沿精确唤醒，零业务期轮询应趋近于 0；高频流式增量应在内存微批聚合为连续页落盘，而不可逆的控制认领才同步刷盘。
5. **前端面板与工程研发飞轮（关键渲染路径字节最小化与静态契约前移）**
   首屏只应传输压缩后的核心壳层字节；UI 调和复杂度应与变化量成正比 $O(|\Delta|)$ 而非 $O(N^2)$；跨模块接口错误应在毫秒级静态类型门禁中拦截，而非靠 660+ 个独立 Python 进程逐一冷启动排错。

---

## 1. 全景优化项优先级总表（OPT-01 至 OPT-52）

按原有分级记录技术优先级，实际开发次序见第 9 节。先处理已有复现的附件和回执错误，同时分段测量接收确认延迟。下表中标有隔离实验的数字描述指定样本，其余目标收益需要在同一硬件、数据和负载下验证。

| 编号 | 维度 | 优先级 | 核心定位（文件与行号） | 源码现状与证据 | 优化目标或验收指标 |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **OPT-01** | MCP 协议 | **P0** | `hub/mcp.py:233-242`、`hub/oauth.py:65-67` | 无显式选项时默认 `view=2`，统一目录可显式选择紧凑视图。全量客户端使用率待测 | 对照两种视图的字节、往返次数及现有客户端兼容性 |
| **OPT-02** | MCP 协议 | **P0** | `agent/filesystem.py:772-862`<br>`shared/contracts.py:114-123` | 同步/异步搜索均无 `context_lines`，且同步搜索丢弃已读字节的 `sha256` 与总行数 | 消除 70%+ “搜完必读”二次文件读取 RTT |
| **OPT-03** | MCP 协议 | **P0** | `shared/coding_contracts.py:81-106`<br>`shared/core_contracts.py:236-263` | `PatchChange` 仅支持整文件 `write` 缺局部 `edit`；`Edit` 缺 `replace_all`/`dry_run` 且成功默认回显 4KB `diff` | 5 文件小改 Output Token 从 ~40.5K 降至 ~620（**-98.5%**），具备原子预检回滚 |
| **OPT-04** | MCP 协议 | **P1** | `agent/core_files.py:156-172`、`shared/mcp_presentation.py:952-973` | 单读和批读均调用 `auto_context`，常规紧凑读响应的字段集合未纳入 `project_knowledge` | 在已知调用方需要时生成知识上下文，分别计量单读、批读与连续分页 |
| **OPT-05** | MCP 协议 | **P1** | `hub/tool_router.py:102-119`<br>`hub/core_tools.py:513-674` | `ToolRouter.resolve` 在判 `UNIFIED_TOOLS` 前于单写 DB 线程无缓存重算 22+ 工具 `factor()`；二分裁剪重跑 ~20 次整包序列化 | 消除每次 MCP 调用在 DB Worker 上的纯 CPU 图归约阻塞 |
| **OPT-06** | MCP 协议 | **P1** | `hub/core_tools.py:806-822`<br>`shared/unified_contracts.py:1141-1165` | `view=3` 下批量读 `files[]` 与非零退出码 `exec` 仍双份传正文；`unwrap_waited` 漏 `project/knowledge/tasks` 等 | 8 文件批读与失败测试堆栈有效净荷翻倍（~22KB → ~46KB） |
| **OPT-07** | MCP 协议 | **P1** | `hub/mcp.py:162, 184, 240`<br>`scripts/mcp_stdio_bridge.py:181` | 强制校验 `text/event-stream` 却零进度/保活帧（与 stdio 桥 12s 超时冲突）；忽略 `notifications/cancelled`；`mcp_tasks` 回退 `view=2` | 消除 12s/30s 假超时重发风暴，打通流式进度与取消传播 |
| **OPT-08** | MCP 协议 | **P1** | `shared/token_estimate.py:55, 152, 228`、`hub/context_usage.py:31-37` | 隔离实验中正文从 24 B 增至 24,000 B，单读估算由 22 增至 9,133，批读一直为 6 | 对可见批读文本计量一次，继续过滤附件和敏感字段。参考费用另行验证 |
| **OPT-09** | Hub 存储 | **P0** | `hub/store.py:175-194, 481-491`、`hub/db_worker.py:19-39` | 与所读数据无关的一次普通提交，使同一读回调执行两次，当时权限版本未变 | 记录重试原因及末轮写锁占用，在覆盖授权依赖后缩小失效范围 |
| **OPT-10** | Hub 存储 | **P0** | `hub/runtime.py:1571-1779, 2131-2630`<br>`hub/operation_output.py:101-138` | `BEGIN IMMEDIATE` 内执行多 MiB JSON+Fernet 加密；派发链路 `_prepare_delivery` 与 `_delivery_packet_current` 双重解密整包；终态 `output` 双写 | 单写事务持锁时长缩短 **75%–95%**，派发控制延迟降 50%+，每条命令少落盘最高 ~350KB |
| **OPT-11** | Hub 存储 | **P0** | `hub/http.py:51`、`hub/app.py:150`、`hub/collaboration/room_attachments.py:204`、`hub/incoming_files.py` | 房间附件允许 8 MiB，默认中间件在 6 MiB + 1 字节已返回 413。大文件导入另有分块调度与写入放大 | 先对齐准确路由的大小契约，再独立测量导入分块的调度次数、WAL 字节及内存 |
| **OPT-12** | Hub 存储 | **P1** | `hub/collaboration/channel_media.py:216, 458-634`<br>`media_cache.py:33-41` | P16 附件“分块加密入库→内存拼接→单写事务整包重加密”双写放大；`row()` `SELECT *` 双读 85MiB 密文；HTTP Range 全包解密 | 落地磁盘分块流式 AEAD CAS + DEK 信封密钥：Range 内存降至 512KB 内，轮换附件 I/O 降 99.9% |
| **OPT-13** | Hub 存储 | **P1** | `hub/runtime.py:2920-2935, 3126-3144`<br>`hub/artifacts.py:179-240, 344-366` | `artifact_chunk`/`native_*` 数据帧逐帧抢占 `run_control` 跑 5 条 SQL；产物每 256KB 重复 3 次只读快照验权 | 512MiB 产物下载减少 2,048 次 `control` 写抢占与 4,096 次只读调度 |
| **OPT-14** | Hub 存储 | **P1** | `hub/runtime.py:225-254`<br>`hub/collaboration/relay_hints.py:137-199` | SSE 全局 `watchers` 无 `space_id` 剪枝引发 $O(W \times E)$ 权限重算；64 路 `relay_hints` 每 5s 走单写线程轮询并易被冲刷为 `resync` | 多空间并发 SSE 鉴权读查询降 90%+，消除 `relay_hints` 后台单写轮询 |
| **OPT-15** | Hub 存储 | **P1** | `hub/gateway/validation.py:18-52`<br>`hub/gateway/service.py:349-516` | 外部网关每次调用 `fork+exec` Python 子进程校验 Schema（上限 4）；`admit` 写事务跑 7 条清理/配额子查询；`visible_tools` 96 次点查 | 每次外部工具调用省 30–150ms，单写调度减半，`tools/list` 降至 1–2 次查询 |
| **OPT-16** | Hub 存储 | **P2** | `hub/keyring.py:194-410`<br>`hub/runtime.py:481-529` · `hub/oauth.py:73-86` | 离线密钥轮换单巨型事务 + `mcp_tasks` 逐行 `DROP/CREATE TRIGGER` 重编数万次 Schema；`_visible_operations` 192 次点查；认证表缺 `expires` 索引 | 实现三阶段零停机渐进密钥轮换与 `incremental_vacuum`；阻塞链查询从 192 次降至 2 次 |
| **OPT-17** | Agent 执行 | **P0** | `agent/workspace_probe.py:156-239`<br>`agent/runner.py:899-961` | 每次只读文件/目录树冷启动全新 Python `ReadWorker` 子进程，争用回退与 `open_workspace` 触发**双倍派生** | 无状态受控热池将本机单读延迟从 30–70ms 压至 **0.8–2.5ms（提速 15–35x）** |
| **OPT-18** | Agent 执行 | **P0** | `agent/runner.py:2374-2418`<br>`agent/owned_process_group.py:21-135` | `_run_process_account` 固定 `sleep(0.05)` 注入 50ms 退出延迟地板；每次清理全扫 `/proc` 或派生 `/bin/ps`；`Workspaces.list` 每工作树跑 4 次 Git | `pidfd_open`/`kqueue` 零回收瞬间唤醒消除 25–50ms 空等；工作树列表提速 75%–85% |
| **OPT-19** | Agent 执行 | **P0** | `agent/core_files.py:189-236`<br>`agent/batch_patch.py:151-200` | `CoreFiles.edit` 单文件 5 次全读 + 百万字符数组；`apply_patch` 与 `engine.mutate` **双重全量备份**并触发 $8N+4$ 次物理 `fsync` | 20 文件补丁 `fsync` 从 164 次降至 ~26 次（**-84%**），备份减半，编辑内存降 95% |
| **OPT-20** | Agent 执行 | **P0** | `agent/filesystem.py:598-673, 696-864`<br>`agent/searches.py:207-270, 353-373` | 全文搜索无 `rg`/字节级预筛，纯 Python 全量解码 + 双重路径 `lstat`；`tree(scope="git")` 跑完 `git ls-files` 仍全盘 `scandir` | 大仓库检索吞吐提升 **15–50x**（纯 Python 路径亦提速 6–12x），`tree` 降至毫秒级 |
| **OPT-21** | Agent 执行 | **P0** | `agent/runner.py:543, 1000, 1050`、`agent/journal.py:142`、`agent/telemetry.py:74`、`hub/runtime.py:2214` | 真实打开项目耗时 33.08 s，Hub 记录 7 次投递，32.26 s 时看到接收阶段。同步 journal 入账会阻塞事件循环 | 分段记录投递与确认延迟，让磁盘等待退出事件循环，保持确认前提交与重复操作恢复 |
| **OPT-22** | Agent 执行 | **P0** | `agent/snapshots.py:36-200`<br>`agent/runner.py:1595-1650` | 持稳定读锁三遍全树扫描（含 `node_modules`/`.venv`）；Linux 逐文件 `fork` 万次 `/bin/cp`；`.venv` 标准自引用 shebang 误拒；ext4 无降级 | macOS 目录级 `clonefile` 与 Linux `FICLONE` ioctl 提速 **50–100x**，修复 `.venv` 误拒并补齐 ext4 Overlay 后端 |
| **OPT-23** | Agent 执行 | **P1** | `shared/secret_output.py:38-69`<br>`shared/audit_redaction.py:34-43` | `credential_output_values` **仅硬编码 5 个变量名**，自定义任务密钥明文落盘且未接入模式脱敏，多密钥后缀回退为 $O(K \cdot L^2)$ | Aho-Corasick 自动机 + 行/PEM 状态机实现动态敏感变量全覆盖与严格 $O(\|T\|)$ 吞吐 |
| **OPT-24** | Agent 执行 | **P1** | `agent/container_runtime.py:164-218`<br>`agent/task_preflight.py:49-104` · `runner.py:1672` | `container_v1` 误拒内部封闭硬链接（pnpm）、单次起 7 个 Docker CLI、命名任务 `requirements` 直接报 409；VPS 执行缺失本机 8MiB 日志与远端 `CODEPIER_RUN_DIR` | 打通容器任务三元预检与 pnpm 硬链接，补齐 VPS 8MiB 脱敏日志与远端独占运行目录（含符号/LSP 温池） |
| **OPT-25** | 协作与会话 | **P0** | `hub/collaboration/coordination.py:92-194, 405-436, 726-962` | `mutation()` 不分首次执行与重放无条件二次全量鉴权+重建视图；`authorize()` 全扫历史审批 JSON；`options()` 内层循环刷 `refresh_principal` | Goal/Work 写锁占用缩短 **45%–55%**，`options/detail` 消除数百次循环查询 |
| **OPT-26** | 协作与会话 | **P0** | `hub/collaboration/events.py:997-1495, 1680-1774` | 房间级 Outbox 唤醒全部 64 订阅逐条反序列化丢弃 63 次；同事务隔 47 行重复鉴权；慢回调期间 250ms 空扫主库并串行跑 6 个恢复事务 | 64 成员房间 Outbox 读写放大降 **90%+**，慢回调期间主库 4Hz 空扫归零 |
| **OPT-27** | 协作与会话 | **P0** | `hub/collaboration/dot_relay.py:220`、`channel_router.py:782`、`weixin_delivery.py:1583` | 指定第 124、125 条均失败，整批读取漏掉第 125 条，单独查询可见。投影查询和缓存命中率另测 | 完整合并已获准查看的 ID 及其分项状态，对照单查与合查 |
| **OPT-28** | 协作与会话 | **P0** | `agent/chat_worker.py:1099-1312`<br>`shared/native_cli.py:34-35, 130-137` | 流式每 Token 触发 `FULL fsync` 制造十万级百字节行；8 会话每秒 `SUM(length(data))` 全扫数 GB BLOB；40Hz 空闲轮询；`chat_answer` 全量重放卡写锁 | 50ms/16KB 微批合并使流式 `fsync` 降 **85%–95%**，配额核算变 $O(1)$，审批点查亚毫秒完成 |
| **OPT-29** | 协作与会话 | **P0** | `shared/native_cli.py:49`、`hub/native_cli.py:561, 646, 898, 1050`、`agent/native_sync_flow.py` | 16,384 个变长块样本中，近末尾导出 4 块用 131,095 条 SQLite VM 指令。另有逐帧授权查询、全局缓存失效及 ACK 退避 | 已有复合索引定位后同样返回 4 块，候选方案用 82 条指令。逐批验证授权、恢复和端到端耗时 |
| **OPT-30** | 协作与会话 | **P1** | `hub/collaboration/lifecycle.py:42-191`<br>`channel_router.py:666-766` · `coordination.py:1450` | 微信/Telegram 出站入队不唤醒协程、靠 1s 盲轮询（每秒 `order.backfill`）；纯文本发送 4 次 `store.run`；受管 `admit` 刚加密入库立即解密重加密 | 空闲期渠道轮询趋近于 0，出站首包提速 ~500ms，受管准入消除二次加解密与 4 条 UPDATE |
| **OPT-31** | 协作与会话 | **P1/P2** | `agent/computer.py:180-196`、`agent/computer_process.py`、`shared/computer_media.py` | 桌面启动环境已包含 Windows `SYSTEMROOT` 和 `SystemRoot`。Wayland 会话所需环境、Windows 子进程回收和媒体复制需分别验证 | 做对应平台的启动与回收验收，并实测媒体峰值内存 |
| **OPT-32** | 前端与工程 | **P0** | `web/mcp-apps/build.mjs:12-29`<br>`web/mcp-apps/changes-v1.html:34` · `review.js:24` | 17.3KB 自有审阅代码因传递打入 `zod@4.6.6` 命名空间导出膨胀至 **636,323 B**；硬编码旧工具名 `read`/`process` 在统一目录失灵且 `ttlMs:0` | 2.5KB 原生 `postMessage` 桥替代重型依赖，体积降至 **~20 KB（-96.5%）** 并对齐统一路由 |
| **OPT-33** | 前端与工程 | **P0** | `web/index.source.html:10-62`、`web/lazy-assets.js:31-35` | 入口引用 35 个 JS、16 个 CSS、1 个图标，合计 1,282,240 原始字节。33 个 JS 带 `defer`，按需路由内目前逐项等候 | 按依赖拆分，测冷开、缓存命中、到可交互的耗时和路由失败重试 |
| **OPT-34** | 前端与工程 | **P0** | `hub/static_assets.py:86-127`<br>`web/mcp-apps/build-room.mjs:21-63` | 构建期用 Prettier 反向展开打包产物、`build-core` 禁 minify；`ReleaseAssets` 零 GZip 预压缩，每次升级重传 1.69 MB 明文 | 启动期验签后内存确定性 `gzip(mtime=0)` + 构建紧凑化，静态网络传输削减 **78%–82%** |
| **OPT-35** | 前端与工程 | **P0** | `.github/workflows/ci.yml:68`、`.github/workflows/pull-request.yml:54`、`scripts/regression_plan.py` | 两个显式 Node 步骤均只列两份文件，另两份测试单独运行共 10 项通过 | 让两种命名的测试都进入常规发现。Python 分组与浏览器复用再做单独对照 |
| **OPT-36** | 前端与工程 | **P1** | `web/app.js:1117` · `web/core/dom.mjs:22-28`<br>`web/chat/timeline.js:114` · `collaboration-room/timeline.mjs:379` | `workflows` 漏调和白名单致整树销毁失焦；`dom.mjs` 无 Key $O(N^2)$ 查找；`ChatUI.dirty` 只写不读；协作室每轮两遍 `find` + 200 次整对象 `JSON.stringify` | 修复 `workflows` 闪烁失焦，流式与协作室主线程帧耗时降低 **75%–85%** |
| **OPT-37** | 前端与工程 | **P1** | `shared/execution_policy.py:166, 229-299`<br>`pyproject.toml:29-41` · `ruff.toml:14-21` | `InvocationInspector` 未对纯语法校验 `node --check` 短路致 `web/app.js` 爆 20,000 AST 节点预算报 403；Mypy 仅覆盖 **9/441 文件（2.04%）** 且 `silent` 吞错误 | 根治大 JS 语法检查 403 误拦；首期将 `shared/*` + `hub/api/*` 纳入 `normal` Mypy 覆盖 >25% 核心契约 |
| **OPT-38** | 前端与工程 | **P2** | `web/workspace.css:285-400, 803-844`<br>`Dockerfile:21-31` · `.github/workflows/ci.yml:410` | `workspace.css` 尾部桌面规则跨过 `@media` 覆盖窄屏布局且 `css_override_report.py` 未入 CI；`Dockerfile` `RUN chmod -R` 致镜像源码层 Copy-up 翻倍；`oidc` 缺 `--require-hashes` | 消除窄屏样式倒置，多阶段准备使镜像应用层缩减 **~55%**，闭合哈希供应链门禁 |
| **OPT-39** | Agent 文件读取 | **P1** | `agent/workspace_probe.py:283`、`agent/core_files.py:20-95` | 2 MiB 文件读取 8 页，返回 256 KiB，应用层共读 16 MiB | 为显式固定版本的连续阅读建立有界游标，保留严格 SHA 读取 |
| **OPT-40** | Agent 命令启动 | **P1** | `agent/runner.py:1683`、`agent/run_directories.py:95` | 128 个近期目录连续清理 8 次，共读取 1,024 个标记 | 按项目合并后台清理，依据 journal 与活动进程核对崩溃遗留目录 |
| **OPT-41** | Agent 传输 | **P1** | `agent/send_priority.py:22, 87`、`agent/runner.py:175` | 一个产物帧先到，后加入 64 个控制帧，产物排第 65 位 | 保持紧急确认的优先权，同时验收数据帧的最大等待与实际进度 |
| **OPT-42** | MCP 协议 | **P1** | `hub/runtime.py:842-850, 1486-1492`<br>`hub/core_tools.py:395` | 整树 `tree` 在 Agent 侧资源等待 73.4 s，期间两次轮询只返回 `queued`。1,423 B 等待回执中三份导航信息占 740 B（52%），操作 ID 重复 5 次 | 回执带上阶段摘要，导航合并为一份；统计连续轮询的累计字节与模型的后续决策 |
| **OPT-43** | Agent 调度 | **P1** | `agent/resource_queue.py:376`<br>`agent/scheduler.py:209-249` | 同例 `blocked_count=1` 而 `blocked_by=[]`，阻塞者对调用方不可见、不可取消；根路径 tree 与项目内任一路径声明冲突 | 脱敏阻塞描述与 `max_queue_ms`；复核 Git 索引列表的快照语义，测并发写入下根目录 tree 的等待 P95 |
| **OPT-44** | Agent 工作区 | **P1** | `agent/filesystem.py:40`、`agent/snapshots.py:92`<br>`scripts/benchmark_hub_agent.py:805` | 项目目录 16 GB，已忽略的 `.work/` 占 11 GB、119,834 个文件；tree/search、快照、baseline、Git 四套排除规则互不一致 | 共享排除策略，验收产物外置并按期限回收；测项目根的扫描条目数与快照规模 |
| **OPT-45** | 工程效能 | **P1** | `shared/core_contracts.py:629`<br>`shared/query_contracts.py:49` | 导入期两次推导输出 schema，cProfile 中占 `shared.contracts` 导入的 88%（204,487 次 `deepcopy`）；无 profiler 时自身 156–165 ms，`import hub.app` 共 580 ms | 单次推导或构建期冻结并做 SHA 校验，导入降到 40 ms 内且 schema 逐字节一致 |
| **OPT-46** | 运维韧性 | **P1** | `hub/api/system.py:33`<br>`deploy/k3s/deployment.yaml:173-193` | `/healthz` 排入单写队列执行，同时承担启动、就绪、存活三种探针；无认证返回版本与写错误计数 | 拆分 `/livez` 与 `/readyz`，写 Worker 持锁 120 s 时 Pod 不被重启 |
| **OPT-47** | 运维韧性 | **P1** | `hub/__main__.py:29-52`<br>`docs/DEPLOYMENT.md:166-171` | 备份 zip 同时包含数据库和 `master.key`；副本不做 `integrity_check`；没有 `restore` 子命令和恢复演练 | 密钥分离或加密归档，发布前校验，新增 `restore --verify` 并写明 RPO/RTO |
| **OPT-48** | Hub 存储 | **P2** | `hub/store.py:22-23, 107-113` | 未设置 `journal_size_limit`、`wal_autocheckpoint`，运行期无主动检查点，WAL 峰值后不收缩（推断，待现场数据） | 先暴露 WAL 字节数、检查点结果与最长读事务，再定限额与检查点策略 |
| **OPT-49** | 运维韧性 | **P2** | `agent/runner.py:2668-2688`<br>`deploy/k3s/deployment.yaml:107-110` | 单副本 `Recreate`；在线 Agent 的重连延迟重置为 1 s + U(0,1)，Hub 重启后在 1–2 s 内集中重连 | `going_away` 提示、decorrelated jitter 与握手令牌桶，测重启后首分钟的写队列深度 |
| **OPT-50** | 原生会话 | **P2** | `agent/native_windows.py:266-281, 412` | Windows 原生会话空闲时，读取线程与命令循环均按 10 ms 轮询，后者约每秒查询 SQLite 100 次 | 事件驱动唤醒并保留低频兜底，在 Windows 实机测空闲 CPU 与回显延迟 |
| **OPT-51** | 工程熵 | **P2** | `hub/mcp.py:60`、`agent/runner.py:989, 1687`<br>`shared/core_output_schemas.py:238` | 123 个函数超过 150 行、19 个超过 300 行；`mcp.handle` 689 行，约 184 个分支节点 | 方法分派表与按阶段拆分，Ruff `C901`/`PLR0915` 以当前值为基线只降不升 |
| **OPT-52** | 工程熵 | **P2** | `docs/`<br>`scripts/check_repository_contracts.py` | 141 份文档共 2.5 MB；1,161 处路径引用中 39 处指向已删除文件，372 处行号引用会随改动漂移 | 符号锚点与 CI 校验，审计稿归档并建立索引 |

---

## 2. 维度一：MCP 协议层、统一工具契约与大模型上下文/往返效率

### OPT-01 [P0] · 默认展示视图与统一目录的紧凑响应

- 相关实现位于 `hub/mcp.py:233-242`、`hub/oauth.py:65-67` 与 `shared/unified_contracts.py` 的 `present_unified`。
  - `view = request.query_params.get("view", "2")`。统一目录、`view=3` 和精简输出同时满足时会选择紧凑展示分支。
  - OAuth 将受保护资源标识设置为 `self.public_url() + "/mcp"`。资源标识和实际请求的展示参数分别处理。当前资料支持判断默认行为，真实客户端的视图占比仍需按客户端与入口采样。
  - 原稿记录的响应样本中，默认 `open` 返回 `4,538 B`（`view=3` 仅 `1,290 B`）；同一份源码同时塞入 `result.content[0].text`（二次 JSON 转义）与 `result.structuredContent`，使 `49,152 B` 响应预算被腰斩至约 `23.5 KiB`，读取 `37 KB` 的 `hub/mcp.py` 必须拆成 2 轮 RTT。
- 多数默认请求沿用兼容响应，文本和结构化字段可能重复传输相同内容。观察实际请求的视图选择、响应字节和续页次数，可估算切换默认值的影响。
- **无损改造方案**：
  - `hub/unified_tools.py` 为统一目录生成工具描述。先比较显式选择 `view=2/3` 时单读、批读、失败输出及恢复提示的行为，再决定统一目录的默认设置。
  - 可按 `_meta["io.modelcontextprotocol/clientInfo"]["name"]` 维护需要 `mirror=full` 的极少数特殊宿主回退表，并保留 `?view=2&text=json&mirror=full` 与环境变量 `CODEPIER_MCP_DEFAULT_VIEW` 作为零停机回退门控；权限重验与 SHA 逻辑零改动。

### OPT-02 [P0] · 搜索缺失上下文行且同步搜索丢弃已读文件 `sha256`

- **源码证据**：`shared/contracts.py:114-123`（`Search`）、`shared/contracts.py:498-507`（`SearchStart`）、`agent/filesystem.py:772-862`（`FileEngine.search`）、`agent/searches.py:189-256`（`Searches.advance`）。
  - `FileEngine.search` 第 808 行已调用 `data = self.read_bytes(p)` 安全读入完整文件并解码为行数组，但第 825–831 行仅输出孤立单行 `{"path": rel, "line": n, "text": line[:1600], ...}`，既不支持 `context_lines`，也直接丢弃了内存字节零成本可得的 `digest(data)` 与 `len(lines)`。
- **第一性原理根因**：单行命中（如函数签名或常量引用）几乎不足以支撑上下文理解或构造唯一匹配的 `old_text`，迫使 70%–80% 的搜索后必须紧跟一轮 `query(action="file")` 重读同一批磁盘文件。
- **无损改造方案**：
  - 在 `Search` 与 `SearchStart` 新增可选参数 `context_lines: int = Field(default=0, ge=0, le=10)` 与 `include_sha256: bool = False`（当 `context_lines > 0` 时默认附带文件 `sha256` 与 `total_lines`，默认 `0` 完全兼容旧调用与旧 Agent）。
  - 同文件相邻命中的上下文窗口自动合并为连续 `hunks`；`view=3` 的 `_compact_search` 文件头输出 `path\t@sha256\tL总行数`，命中行标 `n:`、上下文行标 `n-`，统一受 `READ_BATCH_PAYLOAD_BYTES` 预算裁剪。由于 `read_bytes(p)` 已执行完整软硬链接与保护路径核验，返回的 SHA 与 `query file` 同源同强度。

### OPT-03 [P0] · 多文件补丁 `PatchChange` 缺失局部替换 `action="edit"` 与编辑契约割裂

- **源码证据**：`shared/coding_contracts.py:81-106`（`PatchChange`, `ApplyPatch`）、`shared/core_contracts.py:236-263`（`Replacement`, `Edit`）、`agent/batch_patch.py:66-148`、`agent/core_files.py:182-235`、`shared/mcp_presentation.py:1039-1056`。
  - `PatchChange.action` 仅为 `Literal["write", "delete", "move"]`，不支持局部替换 `action="edit"`。跨 5 个 30KB 文件同步改一处签名，要么发 5 次独立 `act(action="edit")`（丧失跨文件原子预检与自动回滚），要么走 `changes` 的 `write` 全量覆写（逼迫模型在入参吐出 150KB 完整源码，消耗约 **40,000 个高价慢速 Output Token**）。
  - 同时，`Replacement` 删掉了 `replace_all: bool = False`；单文件 `Edit` 第 261 行显式禁止 `dry_run`；成功编辑默认回传最多 4,000 字符 `diff` 将模型刚发送的修改重复回显。
- **第一性原理根因**：违背大模型“读宽写窄（输入廉价、输出极贵）”的非对称算力特性。
- **无损改造方案**：
  - 将 `CoreFiles.call` 保留 BOM 与混合 CRLF/LF 的精确非重叠替换抽为纯函数 `apply_replacements(before_bytes, edits) -> bytes`；扩展 `PatchChange.action` 支持 `"edit"` 与 `edits: list[Replacement] | None = None`。在 `batch_patch.py::prepare` 阶段对校验过 `expected_sha256` 的内存字节执行预替换——**任一文件任一 `old_text` 失配或不唯一，在触碰磁盘任何字节前整批原子失败返回**。
  - 在 `Replacement` 恢复 `replace_all: bool = False`（或更精确的 `expected_count: int | None = None`），开放单文件 `Edit.dry_run`；在 `view >= 3` 且非 `dry_run` 时默认仅回 `{path, sha256, backup_id, added_lines, removed_lines}`，`args.include_diff=True` 时再附带完整 `diff`。

### OPT-04 [P1] · 剔除 `CoreFiles.call("read")` 幽灵知识全表扫描并补齐符号签名

- 相关实现位于 `agent/core_files.py:156-172`、`agent/project_knowledge.py:599-765`、`shared/mcp_presentation.py:952-973` 与 `shared/core_output_schemas.py`。符号提取另见 `agent/symbols.py`。
  - `CoreFiles.call` 对单读、批读及分页调用 `auto_context`。有已建地图时，`query` 遍历候选条目，对选中的至多 6 条重新读取并验证 SHA。常规紧凑投影按 `OUTPUT_SCHEMAS["read"]` 筛选字段，本次复核确认其中没有 `project_knowledge`。完整输出及原生调用方的依赖需分别核对。
  - 此外，`agent/symbols.py` 提取 Python/JS/TS 符号时丢弃了函数参数签名与返回注解，且不支持多路径批量提取。
- 让调用方明确表达所需上下文，在生成前作选择。可按地图版本复用结构索引，逐次验证所选路径与 SHA。函数签名和多路径符号提取独立验收。文件内容本身的跨页重复读取见 OPT-39。

### OPT-05 [P1] · `ToolRouter.resolve` 静态 Schema 图归约缓存与前缀和二分裁剪

- **源码证据**：`hub/mcp.py:369`、`hub/tool_router.py:25-48, 102-119`、`shared/contracts.py:1671-1743`、`shared/core_output_schemas.py:258-621`（`factor`）、`hub/core_tools.py:513-674`（`bound_batch_response`）。
  - 在 `hub/tool_router.py:113-118` 中，`self.definitions(principal, discovery=False)` 写在 `if name in UNIFIED_TOOLS: return ToolRoute("native", name)` **之前**——导致每一次 `query`/`act` 调用都在单写 SQLite Worker 线程内对 22+ 个底层工具重跑无缓存的 `factor(compact_input)` 与 `factor(outputSchema)`（含多轮树遍历、`deepcopy` 与数百次 `json.dumps`），算完立即丢弃！
  - `bound_batch_response` 对 100B 小文件也做两次整包 `deepcopy`，超预算二分裁剪每次迭代调两次整包 `json.dumps`（单请求高达 ~20 次整包编码）。
- **无损改造方案**：对 `shared.contracts.tool_definitions(profile, authorization, schemas)` 使用 `@functools.lru_cache(maxsize=8)` 缓存不可变模板；在 `ToolRouter.resolve` 入口若 `name in UNIFIED_TOOLS` 仅实时刷新 `principal`（及网关启用时核对远端命名冲突）；`bound_batch_response` 入口做 $O(1)$ 字节下界短路，裁剪时预计算每行 UTF-8 编码长度前缀和数组 `prefix_bytes[i]`，一次二分定位行号后仅执行 1 次 `render()` 校验。

### OPT-06 [P1] · 补齐 `view=3` 下批量读文件、非零退出码 `exec` 单流传输与 `unwrap_waited` 白名单

- **源码证据**：`hub/core_tools.py:806-822`、`shared/unified_contracts.py:1141-1165`。
  - `hub/core_tools.py:806-822` 仅识别顶层单字符串 `public["content"]` 且要求 `not rendered.get("isError")` 与 `exit_code == 0`。导致：① 批量读 `{"batch": True, "files": [...]}` 仍双份传输源码，48KB 预算仅装得下 ~22KB；② 测试失败（`exit_code != 0`）最需要完整报错栈时反而退回双份 JSON 腰斩行数；③ `unwrap_waited` 漏掉 `project`、`knowledge`、`skills`、`tasks`、`artifacts`、`worktrees`，慢节点触发 `_wait` 时多套 920B 调度信封并绕过 `_lean_open`。
- **无损改造方案**：为 `batch=True` 增加多段紧凑体 `=== [index] path (offset..end_line) ===\n<content>`，在 `structuredContent` 剥离 `files[i]["content"]` 标记 `content_in="text"`；放宽 `exec` 紧凑条件为 `isinstance(public.get("exit_code"), int) and isinstance(public.get("output"), str)`（保持 `isError` 布尔位不变）；将上述 6 个只读动作纳入终态成功单操作 `unwrap_waited`。

### OPT-07 [P1] · Streamable HTTP 进度保活帧、取消通知接线与 `mcp_tasks` 视图对齐

- **源码证据**：`hub/mcp.py:161-165, 183-186, 239-240`、`hub/unified_tools.py:303-320`、`scripts/mcp_stdio_bridge.py:181`、`hub/mcp_tasks.py:86`。
  - `hub/mcp.py:162` 强制校验 `Accept` 含 `text/event-stream`，却全程只回缓冲式 `JSONResponse`（最长静默挂起 30–60s），而自带的 `mcp_stdio_bridge.py:181` 硬编码单次超时 `min(12, left)` 秒，长操作在 12s 即超时重发最多 6 次；`notifications/cancelled` 在 2026 协议返 404、2025 协议空返 202，未中断 `_wait` 或转发 `runtime.cancel`；`mcp_tasks.py:86` 未透传 `view/text_mode/mirror` 退回旧视图。
- **无损改造方案**：当携带 `_meta.progressToken` 或 `wait > 10` 且客户端接受 `text/event-stream` 时，每 5s 发送 `notifications/progress` 或 `: keepalive\n\n` 并在 `request.is_disconnected()` 时安全退出等待协程（不回滚已投递写操作）；调高 `mcp_stdio_bridge.py` 单请求超时至 `max(20, wait + 5)`；登记 `(principal_key, request_id)` 响应 `notifications/cancelled`；向 `render_exec_v1` 透传当前视图模式。

### OPT-08 [P1/P2] · 修复 Token 估算器 `"files"` 黑名单漏计与缓存计费模型偏差（附 stdio 开放 `unified`）

- 相关实现位于 `shared/token_estimate.py:55, 152, 228`、`hub/mcp.py`、`hub/context_usage.py:31-37` 与 `scripts/mcp_stdio_bridge.py`。
  - `files` 被整字段排除。对同一段普通文本做单读、批读对照，正文从 24 B 增至 24,000 B，单读估算从 22 增到 9,133，批读始终为 6。数字为 CodePier 估算器输出。原稿另指出 JSON 转义、缺项目字段的轮询归属与缓存费用假设，这些分别用样本检验。
  - 另 `mcp_stdio_bridge.py:161, 347` 硬编码禁止 `profile="unified"`，迫使本地 CLI 客户端注入 ~30KB 旧 `core` 目录（`unified` 仅 ~2.6KB）。
- 对已知响应类型只累计 `files[].content` 等可见正文，沿用现有附件、二进制及敏感字段过滤。验收覆盖单读、批读、镜像正文和分页，统计同一正文一次。任务归属、标点权重、费用模型和 stdio 档位分别对照授权与兼容测试。

---

## 3. 维度二：Hub 核心存储、SQLite 并发模型、加解密热路径与外部网关

### OPT-09 [P0] · 以持久化 `security_epoch` 根治 `PRAGMA data_version` 只读伪失效风暴并分流纯读路由

- **源码证据**：`hub/store.py:141-199`（`Store._read_phase`）、`hub/db_worker.py:19-39`（`database_endpoint`）、`hub/api/activity.py:85, 122, 135`、`hub/artifacts.py:262`、`hub/collaboration/api.py:20-260`、`hub/mcp.py:124, 126, 281, 334, 347`。
  - 在独立 Store 中读取固定值 7，同时对普通测试表提交一次更新，`session_revision` 未变化，同一读回调执行了两次。`_read_phase` 最多尝试四轮，末轮可持有写锁。记录普通写入与权限变更各自触发的重试、锁等待和 503 计数，再据实际占比排序。
  - 同时 `database_endpoint` 默认 `read_only=False`，使 `/api/operations`、`/api/audit`（查询）、全部协作 GET 路由、`tools/list`、`tools.resolve` 等纯只读请求全部排队挤占单写线程。
- **严守实时撤权边界的改造方案**：
  - 在 `meta` 表维护单调递增整数 `security_epoch`；当 `Store._security_write`（预编译表名正则）修改任何权限/身份/设备/项目/网关安全表时，在同一写事务内原子递增 `meta.security_epoch`（辅以 SQLite Trigger 防外部直连绕过）。
  - 可在 WAL 快照内取得 `epoch_before`，读后复核权威版本。先列出当前可见性、授权与映射依赖，并覆盖受支持的全部写入方。用跨连接撤权、过期及并发普通写入的用例验证后，再逐端点收窄失效条件。
  - 将上述纯只读端点统一改为 `read_only=True` / `store.run_read`，只读连接 `cache_size` 调至 `-8192`（8 MiB），拒绝审计改为返回协程层 `await asyncio.wrap_future(...)` 避免占用只读池线程等待写锁。

### OPT-10 [P0] · 单写锁外预序列化/预加密、消除派发双重解密与终态 `output` 双写

- **源码证据**：`hub/runtime.py:1571-1779`（`_admit_operation`）、`hub/runtime.py:2131-2450`（`_delivery_packet_current`, `_prepare_delivery`）、`hub/runtime.py:2452-2630`（`complete`）、`hub/runtime.py:1966-1982`（`_purge_computer`）、`hub/operation_output.py:101-138`。
  - **写锁内重计算**：`_admit_operation`、`outputs.receive`、`complete` 均在 `BEGIN IMMEDIATE` 写锁内对最高数 MiB 的请求/结果执行 `json.dumps` + Fernet AES-CBC+HMAC 加密（单次耗时 5–30ms，而 B-Tree 更新仅 0.1ms）。
  - **派发双重解密**：`_prepare_delivery` 刚读取并解密整包 `op["payload"]`（连查 3 次 `projects`），几毫秒后发送前围栏 `_delivery_packet_current` 在高优 `run_control` 的 `BEGIN IMMEDIATE` 内为核验 `vps_ref` 等元字段，**再次从溢出页读取并解密整包 `payload`**（又查 2 次 `projects`）。
  - **结果双写**：命令完成时 `result["data"]["output"]` 与 `operations.output` 保存相同末尾文本，同一份最多 128 KiB 输出被加密两次落盘两份。
  - **媒体清理占锁**：`_purge_computer` 每 15s 在单写事务内重复执行 `CREATE INDEX IF NOT EXISTS` 并循环对最多 64 条 5MiB 截图记录做“解密→JSON解析→擦除→重加密”。
- **严守 ACID 的改造方案**：
  - 利用运行期 `store.cipher` 只读不可变特性，在进入 `store.run` / `run_control` 前于线程池预计算 JSON 编码、指纹及候选密文令牌；`BEGIN IMMEDIATE` 内仅做 CAS 状态机条件校验与密文绑定写入。
  - `_prepare_delivery` 返回不可变轻量派发摘要（含 `vps_ref`、权限入参摘要与 `payload` 哈希，**绝不缓存授权结论**），`_delivery_packet_current` 省去 `SELECT payload` 与二次解密，直接以实时数据库状态复核全部权限。
  - `complete()` 对与 `output` 完全一致的 `result["data"]["output"]` 存轻量哨兵，读取时由现有 `Runtime.operation()` 的 `output_page` 逻辑透明回填；`_purge_computer` 拆为“`run_read` 取候选 → 锁外解密擦除重加密 → 短事务 `WHERE id=? AND result=?` CAS 写回”。

### OPT-11 [P0] · `BodyLimit` 流式直通白名单与 `incoming_files` 控制面/数据面解耦

- **源码证据**：`hub/http.py:92-158`（`BodyLimit`）、`hub/app.py:143`、`hub/collaboration/room_attachments.py:25, 206`、`hub/incoming_files.py:795-822, 865-990`。
  - 房间处理器声明 `MAX_UPLOAD = 8 * 1024 * 1024`。用同一默认 `BodyLimit` 的内存 ASGI 探针验证，6 MiB 能到达下游，6 MiB + 1 字节、7 MiB、8 MiB 均在下游之前返回 413。有 `Content-Length` 和逐块接收两种输入结果相同。
  - `incoming_files` 每个 256 KiB 分块在 `body()` 循环内外触发 **至少 7 次单写线程 `store.run`**，并将 256 KiB 原始块编码为 341 KiB Base64 再加密为 ~455 KiB Fernet 密文塞进 `operations` 主表——导入单个 512 MiB 文件会产生 **~930 MiB SQLite WAL 写入**与 4,096 条 `operations`/`audit` 行膨胀。
- 首批只对确切的房间上传路由统一为 8 MiB，保留并发数、累计内存预算和中断释放。集成验收覆盖 6 MiB + 1、8 MiB、8 MiB + 1 以及上传中断。后续单独测量 `incoming_files` 的分块调度和 WAL，再评估限量流式接收、临时块落盘与准入复核。

### OPT-12 [P1] · 推进 P16 渠道/面板附件的内容寻址磁盘分块流式 AEAD CAS

- **源码证据**：`hub/collaboration/channel_media.py:216, 458-634`、`channel_media_router.py:35, 157-326`、`room_attachments.py:97`、`media_cache.py:33-41`、`collaboration/api.py:98-115`。
  - `materialize()` 将 256 KiB 分块逐个加密写入 `channel_attachment_chunks`，同时在内存列表保留全量明文 `b"".join(chunks)`，再于 `commit()` 的单写事务内整包二次加密为最高 ~85 MiB 单列 TEXT 写入 `channel_attachments.content`。
  - HTTP Range 下载前后两次验权调用 `store.run(row)` 均执行 `SELECT *`，在单写线程把最高 85 MiB `content` 加载进内存两次；`decode_range` 对任意 `Range: bytes=0-65535` 都必须全包解密并重算全量 SHA-256。
- **两步演进方案**：
  1. **立即可做（零 Schema 变更）**：将三处 `row()` 改为显式元数据列投影（排除 `content`/`private_source`），并将下载验权切至 `store.run_read`。
  2. **磁盘分块 CAS 架构**：落盘于 `state_directory / "media_cas" / sha256[:2] / sha256`，按固定 256 KiB 切块，每块采用独立随机 DEK + AES-256-GCM（AAD 绑定 `(attachment_sha256, chunk_idx, total_chunks, is_final)` 防重排截断），DEK 经主密钥 KEK 信封加密存入 SQLite。先锁外写盘 `fsync`、再短事务置 `ready`（孤儿块由持久清理队列回收，空间配额仍按逻辑引用独立计量）。HTTP Range 仅解密命中分块（内存 `<512 KiB`），且密钥轮换只需重加密几十字节 DEK，磁盘大文件零重写。

### OPT-13 [P1] · 数据帧基于 `session_revision` 快速放行与产物下载快照合并

- **源码证据**：`hub/runtime.py:2920-2935, 3126-3144`、`hub/artifacts.py:179-240, 344-366`。
  - `agent_socket` 对每一帧 `artifact_chunk` / `native_reply` / `native_sync` 都调用 `store.run_control(_receive_agent_frame)` 跑 4–5 条 SQL 查库并写 `last_seen`，挤占真正的控制回执槽位；`artifacts.download` 每传输 256 KiB 连续执行 3 次只读查库（`snapshot` → `chunk` 内 `online` → 写出前 `snapshot`）。
- **无损改造方案**：对纯数据面帧，当连接活跃、距上次 `_persisted_last_seen < 12s` 且 `store.session_revision` 自上次验权未变时，直接在事件循环更新内存 `last_seen` 路由分发（一旦发生任何撤权/停用，`session_revision` 立即递增触发实时查库断连）；合并 `artifacts.download` 为每分块写出前唯一一次 `store.run_read(snapshot)`（内含设备在线核验）。

### OPT-14 [P1] · SSE 按 `space_id` 路由剪枝与剥离 `relay_hints` 5 秒单写轮询

- **源码证据**：`hub/runtime.py:225-254`、`hub/api/activity.py:194-295`、`hub/collaboration/relay_hints.py:137-199`。
  - `runtime.publish` 无受众分桶向最多 164 个 `watchers` 全量广播，触发 $O(W \times E)$ 次 `project_permissions` 多表查询；64 路 `relay_hints` 每 5s 超时都走单写线程 `store.run(snapshot)`，且 `maxsize=1` 队列极易被其他空间的高频 `output`/`trace` 事件冲刷成 `resync`。
- **无损改造方案**：`publish` 附带仅用于剪枝的 `audience={"space_id": ...}` 分桶索引（命中桶仍走 `snapshot` 权威验权，绝不越权放行）；将 `relay_hints` 从全局 `watchers` 拆出改为协作表提交精确唤醒并切至 `store.run_read`；15s 空闲心跳在 `session_revision` 未变且会话未过期时免除全量项目表重扫。

### OPT-15 [P1] · 外部 MCP Gateway 常驻受限校验进程池与读写分流

- **源码证据**：`hub/gateway/validation.py:18-52`、`hub/gateway/service.py:349-516`、`hub/gateway/policy.py:72-122`、`hub/gateway/remote.py:114-150`。
  - 每次外部工具调用通过 `create_subprocess_exec` 冷启动 1–2 个 Python 子进程跑 JSON Schema 校验（固定耗费 30–80ms，并发硬上限仅 4）；`admit` 在写事务内每次执行 3 条过期 `DELETE` + 4 条配额 `COUNT(*)`；`visible_tools` 串行发起最多 96 次点查并重复计算全套 Schema SHA-256；`Session.operation` 持 `condition` 锁执行网络 `initialize()`。
- **无损改造方案**：将 `validate_worker.py` 升级为 2–4 个常驻无特权受限子进程池（JSON 行管道通信，超时 8s 精准强杀单槽位并拉起替补）；`prepare` 与 `before_send` 切 `run_read`，`complete` 锁外加密，`admit` 过期清理按 60s 或配额近满节流；`visible_tools` 改为单条三表 `JOIN` 并按不可变 `(account_id, version, catalog_hash)` 缓存纯工具指纹。

### OPT-16 [P2] · 三阶段在线渐进式密钥轮换、认证表索引补齐与 `_visible_operations` 批量化

- **源码证据**：`hub/keyring.py:194-410`、`hub/operation_history.py:131-359`、`hub/operation_archive.py:39-78`、`hub/runtime.py:481-529`、`hub/call_log.py:255-305`、`hub/oauth.py:73-86`、`hub/store.py:37-40`。
  - `rotate_key` 要求停机并在单巨型事务内重写 46 个密文列，且更新 `mcp_tasks` 每行每列都执行 `DROP TRIGGER -> CREATE TRIGGER -> UPDATE -> DROP TRIGGER -> CREATE TRIGGER`（5,000 条任务触发 **40,000 次 DDL 与全库 Schema 重解析**）；归档后缺乏 `auto_vacuum=INCREMENTAL` 只能停机 `VACUUM`。
  - `_visible_operations` 对最多 64 个阻塞 ID 串行跑 `192` 次点查；`tokens`/`oauth_codes`/`oauth_requests` 的 `expires` 列与 `login_attempts(ip, at)` 缺索引，每次 `/oauth/*` 请求触发 4 张表全扫。
- **无损改造方案**：
  - 依托 `CipherSuite` 已有的 `cp1:<key_id>:` 多代读取能力，拆分为：① 在线原子热重载新写密钥；② 后台小批次（`batch=50`）锁外重加密 + `WHERE pk=? AND col=?` CAS 乐观写回，并用事务级白名单表 `mcp_task_rekey_allowlist` 替代逐行 `DROP/CREATE TRIGGER`（全程零 DDL）；③ 旧代清零后分批校验退役；新库启用 `PRAGMA auto_vacuum=INCREMENTAL`。
  - 迁移补齐 4 个认证过期索引并按 60s 节流清理；`_visible_operations` 合并为 2 条批量 `IN` / `GROUP BY` 查询；`call_log` 在无 `q` 搜索时省去 `LEFT JOIN` 计数。

---

## 4. 维度三：Agent 本机执行引擎、文件系统 I/O、流式脱敏与隔离运行时

### OPT-17 [P0] · `ReadWorker` 无状态受控热工作池（消除单次读冷启动与争用双倍派生）

- **源码证据**：`agent/workspace_probe.py:20-69, 156-239, 313-350`、`agent/runner.py:899-961`。
  - 每次 `read`/`fs_tree`/`fs_search`/`project_knowledge_query` 均通过 `sys.executable -I -c ...` 冷启动全新 Python 进程（macOS/Windows 固定耗时 `25–65 ms`，而 SSD 读 16KB 仅 `0.2 ms`）；当 `scheduler.try_reserve` 争用失败或调用 `open_workspace` 时，先销毁第 1 个探测子进程，获槽后再冷启动第 2 个子进程（双倍派生）。
- **第一性原理根因**：独立子进程的真实安全目标是 NFS/SMB 挂死时的**可 `SIGKILL` 回收性**，而非 OS 沙箱；洁净无状态进程无需每次请求后自毁。
- **严守二次重验的改造方案**：建立容量 `min(read_limit, 4)` 的**无状态临时租赁热池（Stateless Ephemeral-Lease Warm Pool）**：子进程启动仅预导入只读模块并驻留空目录，**零跨请求缓存任何 `config`、`allowed_roots`、路径或知识数据**；每次借出分配单调 `lease_epoch`，完整保留准入前、获槽后、`mark_running` 后的三阶段 `root` 与权限二次重验帧；任何超时（15s）、取消或异常立即 `SIGKILL` 剔除绝不归还，正常服务满 256 次平滑轮换。

### OPT-18 [P0] · 内核事件驱动退出监听（消除 50ms 轮询地板与全机 `/proc` / `ps` 扫描税）

- **源码证据**：`agent/runner.py:2374-2418`、`agent/owned_subprocess.py:37-69`、`agent/owned_process_group.py:21-135`、`agent/workspaces.py:218-250`。
  - `_run_process_account` 中 `exited()` 使用 `await asyncio.sleep(0.05)` 轮询退出，令 2ms 完成的短命令在管道 EOF 后仍空等最高 50ms；`finally` 无条件调 `live_group_members()`，Linux 遍历全机 `/proc/*/stat`，macOS 额外派生 `/bin/ps -x -g <pgid>`。`Workspaces.list()` 对每个工作树串行跑 4 次 Git 子进程，20 个工作树累计空等约 4 秒并扫描 80 次进程表。
- **严守 PGID 防复用的改造方案**：Linux 使用 `os.pidfd_open(pid, 0)` + `loop.add_reader`，macOS 使用 `select.kqueue` `KQ_FILTER_PROC | KQ_NOTE_EXIT` 微秒级唤醒退出事件，**继续保持 `WNOWAIT` 钉住僵尸组长直至进程组完全清理完毕再 `waitpid`**（PGID 防复用安全性 100% 不变）；Linux `/proc` 改用 `os.scandir` 栈字节解析；`Workspaces.list()` 合并为每工作树单次 `git status --porcelain=v2 --branch --untracked-files=all`。

### OPT-19 [P0] · 消除单文件编辑 5 次全读、`apply_patch` 双重全量备份与每文件 8 次 `fsync`

- **源码证据**：`agent/core_files.py:189-236`、`agent/filesystem.py:890-1118`（`FileEngine._mutate`）、`agent/batch_patch.py:151-200`、`agent/journal.py:230-260`。
  - 单次 `CoreFiles.edit` 全量读取目标文件 **5 次**（其中 `_mutate` L932 与 L960 之间目标文件未动，L932 纯属冗余），且逐字符构造 `positions: list[int]`（1 MiB 文件瞬时分配 ~200 万对象 / ~60 MB 堆内存）。
  - `apply_patch` 先在 L163–L182 为 $N$ 个文件逐个写全量备份 + `fsync` + 独立事务，随后在 L186–L198 循环中每改 1 个文件重写 1 次 `manifest.json`（2 次 `fsync`）并调用 `engine.mutate()`——而 `mutate()` 在 L921–L929 **又无条件新建 `backup_id` 生成第二份全量备份**！导致 $N$ 文件批补丁产生 $2N$ 份备份与 $8N + 4$ 次物理 `fsync`。
- **严守崩溃幂等的改造方案**：新增 `Journal.add_backups_batch`（所有备份文件落盘后仅执行 1 次 `fsync_directory(backups)` + 单事务入库），`_mutate` 接收 `prebuilt_backup_id` 跳过二次重复备份并与 `manifest` 严格对齐；`manifest` 仅在 `prepared` 与终态各原子刷盘一次；删除 `_mutate` L932 冗余全读；纯 LF 文件零拷贝跳过 `positions` 构造，CRLF 文件仅存行尾 `\r` 偏移表并用 `bisect` 映射。

### OPT-20 [P0] · 大仓库搜索“不可信 `rg` 候选召回 + `FileEngine` 精验”与 `fs_tree` 内存前缀树裁剪

- **源码证据**：`agent/filesystem.py:221-235, 519-673, 696-864`、`agent/searches.py:207-270, 353-373`。
  - 当前无任何原生字节搜索加速：对最多 20,000 个文件做两遍逐级父目录 `is_symlink()` + `resolve()`，全量读入 Python 解码 UTF-8 并 `splitlines()`；`tree(scope="git")` 跑完 `git ls-files` 后仍对物理磁盘执行全量 `os.scandir`（被忽略目录也会消耗扫描预算并在出入栈重复校验两次）。
- **零信任加速方案**：
  - 本机存在 `rg`（或 `git grep`）时，仅以无配置参数（`rg --files-with-matches --null --fixed-strings --no-config --no-ignore-vcs`，绝不带 `-L`）召回候选相对路径；**所有召回路径必须走回 `FileEngine` 原有的 `path()`、`protected()`、`O_NOFOLLOW`、`st_nlink == 1` 与 `read_bytes()` 权威精验**。无 `rg` 时在 `bytes` 层先做 `needle_bytes in data` 或字节正则预筛，命中文件才解码 UTF-8，并在单次扫描内去重已验父目录链。
  - `tree(scope="git")` 当 `git ls-files` 未截断时，直接由内存前缀树生成当前分页节点，仅对入选本页（`limit <= 100`）的条目执行磁盘 `path() + lstat()` 核验。

### OPT-21 [P0] · 接收确认、journal 入账与事件循环的磁盘等待

- `Agent.handle` 在事件循环中同步调用 `journal.start`。新操作在该处校验指纹并写 SQLite，随后 `phase` 同步记录遥测，再发送 `accepted`。重复 call 和 probe 在收包循环里等待 `report_status`。定位见 `agent/runner.py:543, 1000, 1050`、`agent/journal.py:142` 和 `agent/telemetry.py:74`。
- **真实调用轨迹。** 打开项目用时 33.08 s。以 Hub 收到请求为零点，首次 `dispatched` 在 0.01 s，第七次在 30.98 s，32.26 s 时看到 Agent 的接收阶段。Agent 自身单调时钟从 `accepted` 到 `result_ready` 为 218 ms。另一条 Node 测试操作耗时 26.54 s，Hub 记录 5 次投递，Agent 到 `persisting` 为 441 ms。`hub/runtime.py:2214` 的投递记录早于 socket 写入完成，这些样本仍需区分发送等待、节点入账与确认回程。
- **隔离实验。** 在已有回执的真实 `handle` 路径占用 journal 锁 120 ms，计划于 10 ms 触发的计时器实际在 121.6 ms 触发。将同一 `journal.start` 交给项目已有的 `disk_call` 后为 10.4 ms。此实验确认阻塞机制，现场数十秒的具体归因仍需分段观测。
- 按同一操作 ID 和尝试序号记录 Hub 发送锁等待、socket 写入完成、Agent 收包至 journal 提交、确认帧排队以及 Hub 收到确认。耗时在各端使用本机单调时钟。
- 入账交给可等待取消收尾的磁盘通道，并在成功提交后发 `accepted`。中间遥测和高频 stdout 可有界合并，保留最终输出落盘和 journal 恢复顺序。验收包括慢盘、断线、丢确认和重复投递，同一操作只能产生一次副作用。

### OPT-22 [P0] · 快照隔离目录级 `clonefile` / `FICLONE` ioctl、`.venv` shebang 重写与 ext4 Overlay 后端

- **源码证据**：`agent/snapshots.py:36-200`、`agent/runner.py:1595-1650`。
  - 在持有项目稳定读锁期间执行**三遍全树遍历**（含数万文件的 `node_modules` 与 `.venv`）：① Linux 对**每一个文件**派生一次 `subprocess.run(["cp", "--reflink=always", ...])`（3 万文件触发 3 万次进程 `fork`）；② macOS 逐文件而非按顶层目录调 `clonefile`；③ 发现 `.venv/bin/pytest` 标准自引用 `#!.../.venv/bin/python` 直接抛 `SNAPSHOT_UNSAFE` 拒绝快照；④ 标准 Linux ext4 直接报 `SNAPSHOT_UNSUPPORTED`。
- **严守隔离边界的改造方案**：macOS 对顶层子项直接执行目录级 `clonefile(2)`（5 万文件降至 `<10ms`）；Linux 用 `O_NOFOLLOW` + `fcntl.ioctl(dst_fd, FICLONE, src_fd)` 进程内零 `fork` 克隆，并将预检与克隆合并为单遍扫描（删去第 3 遍冗余重扫）；对精确指向项目内 `.venv/bin/python*` 的脚本在快照副本内 COW 重写为快照 `.venv` 路径（仍严拒外部未知路径与 editable `.pth`）；ext4 无 reflink 时在满足非特权 `unshare -Ur` + `overlay`/`fuse-overlayfs` 且源树冻结写条件下提供显式 `linux_overlay_v1` 后端（否则维持失败关闭，绝不退回慢速全拷贝）。

### OPT-23 [P1] · 两级流式输出脱敏管线（突破 5 个硬编码变量名限制）

- **源码证据**：`shared/secret_output.py:11-69`、`shared/audit_redaction.py:34-43`、`agent/runner.py:2248-2256`。
  - `credential_output_values` 仅硬编码提取 `SSHPASS` 与 4 个 `GH_*/GITHUB_*` 变量，用户在任务/运行时传入的 `OPENAI_API_KEY`、`DATABASE_URL` 等敏感值若被打印会明文落入 `output.redacted.log`，且未接入通用模式脱敏，多密钥后缀回退为 $O(K \cdot L^2)$。
- **无损改造方案**：自动纳入环境字典中变量名匹配 `_SECRET_KEY` 且长度 $\ge 8$ 的非空值（及连接串密码段）；第一层用单棵 **Aho-Corasick 自动机** $O(|T|)$ 跨块过滤精确密钥，第二层按行缓冲（加 PEM 私钥起止跨块状态机）执行 `audit_redaction.redact_text()`；原始流字节计数与 SHA-256 保持在脱敏前统计不变。

### OPT-24 [P1] · `container_v1` 封闭硬链接放行与三元预检、VPS 8MiB 日志留存及符号/LSP 温池

- **源码证据**：`agent/container_runtime.py:164-218, 485-650`、`agent/task_preflight.py:49-104`、`agent/runner.py:1672-1842`、`agent/ssh.py:30-118`、`agent/symbol_worker.py:38-137`、`agent/lsp_navigation.py:77-171`。
  - `inspect_project_mount` 无条件拒绝任何 `st_nlink != 1` 文件（误杀 pnpm 仓库），单次命令串行跑 7 个 Docker CLI，且 `container_v1` 下声明 `requirements` 的命名任务直接报 409 `TASK_PREFLIGHT_UNSUPPORTED`。
  - `exec(target="vps:...")` 未创建本机 `OperationLog`（超 50KiB 日志永久截断丢失）且远端无 `CODEPIER_RUN_DIR`。
  - `symbol_worker` 一文件一 Python 进程，`lsp_navigation` 一查询一语言服务实例。
- **无损改造方案**：
  - 在 `inspect_project_mount` 统计树内 `(st_dev, st_ino)` 出现次数 `seen_links[ino]`，若 `seen_links[ino] == st_nlink` 则数学证明硬链接 100% 封闭于项目内部予以安全放行；缓存 `docker info`（60s）与不可变 `image@sha256` 的 `image inspect`（保留每次 `create` 后容器 `inspect` 零信任校验）；实现容器任务三元预检（核对 `plan.environment`、挂载源 `files`，对镜像内可执行文件用无挂载 `--network none --read-only --cap-drop ALL` 纯镜像探测并按 `image@sha256` 持久缓存）。
  - 为 `vps:...` 执行统一落盘本机 `0600` `output.redacted.log`，并通过安全 shell 引导头以非 `-p` 原子 `mkdir` 导出远端独占 `CODEPIER_RUN_DIR`。
  - `symbol_worker` 改为批流式帧复用子进程（单文件崩溃隔离自动重启）；`lsp_navigation` 增加按 `(project_id, root_dev_ino, language, config_sha256)` 隔离的 180s 空闲温池（每次复用前重验目录与权限）。

### OPT-39 [P1] · 给固定版本的文件阅读提供分页游标

从 `agent/workspace_probe.py:283` 进入 `CoreFiles.read_one` 后，每页都重新读取完整文件、计算 SHA、解码并拆行。2 MiB 文件连续读取 8 页，每页 512 行，返回正文 256 KiB，应用层总读取 16 MiB。随后改动文件，携带旧 SHA 的请求返回 `SHA_CONFLICT`。读取量按应用调用计数，物理磁盘读取受页缓存影响。

让连续阅读显式绑定不可变版本，按 SHA 保存有容量和期限的快照及行偏移，每次续读复核路径权限。实时读取和编辑继续校验当前文件。验收同版本连续翻页、文件中途修改、权限撤销与游标过期。源码见 `agent/core_files.py:20-95`。

### OPT-40 [P1] · 把历史运行目录回收移出命令启动路径

`agent/runner.py:1683` 在每次本机 `exec` 分配目录前调用 `cleanup_run_directories`。128 个近期完成目录，8 次清理读取 1,024 个标记。模拟推进 30 天后，128 个完成目录均被回收，只有开始标记的目录仍保留。回收条件见 `agent/run_directories.py:95`。

按项目合并后台维护，分批处理并记录进度。遇到只有开始标记的目录，结合 journal 终态、进程存活和恢复状态判断归属。验收并发启动、崩溃重启、长期空闲项目和正在运行的任务，分别计量启动耗时与清理读取次数。

### OPT-41 [P1] · 让等待中的产物和原生会话数据获得发送机会

`agent/send_priority.py:22, 87` 将产物块和原生同步归为大块数据。发锁交接始终先检查控制队列。先排入一个产物帧，再排入 8、32、64 个控制帧，产物分别排第 9、33、65 位。只要控制队列持续非空，现有规则就没有给老数据帧设服务上界。发送入口见 `agent/runner.py:175`。

紧急确认和取消继续优先，对普通进度或可合并状态设置连续发送预算，让等待最久的数据帧获得一次发送机会。控制确认 P99、最老数据帧等待时间及每个活跃同步的字节进度同时验收。此项和 OPT-21 的现场慢请求分别测量。

---

## 5. 维度四：协作中心（Collaboration / Dot / 渠道）与原生会话/桌面控制

### OPT-25 [P0] · `CoordinationService` 统一出口单次投影、历史项目并集物化与批量预取

- **源码证据**：`hub/collaboration/coordination.py:92-194, 203-209, 405-436, 726-1019`。
  - 各写方法内部闭包已算完一次完整 `view()`/`work_view()`，外层 `CoordinationService.mutation()`（L418–435）不分“首次执行”与“幂等重放”，无条件再次重跑 `authorize` 与整套视图查询；`authorize()` 每次 `SELECT spec FROM coordination_approvals` 全扫历史审批反序列化求项目并集；`options()` 在三层循环内反复调 `refresh_principal`；`detail()` 逐行 N+1 查分配表并重复 `json.loads(row["result"])` 3–4 次。
- **保持实时重验的改造方案**：令 `self.c.mutation` 返回 `(result, replayed)`，首次写入在同一把 `Store` 锁内仅在出口投影一次，幂等重放路径 100% 保留实时重鉴权投影；在 `coordination_goals` 物化单调只增的 `historical_project_ids` 列省去历史审批表全扫；`options()` 外提 `refresh_principal`，`detail()` 用 `WHERE work_id IN (...)` 批量预取分配字典。

### OPT-26 [P0] · `EventService` Outbox 窄播索引下推、同事务鉴权合并与消除 250ms 空扫

- **源码证据**：`hub/collaboration/events.py:997-1495, 1680-1774`。
  - `discover_deliveries` 仅按 `room_id` + `seq > scan_seq` 扫描：64 成员房间每出 1 条单播唤醒事件，全部 64 个订阅都被唤醒执行 `authorize`、拉 100 条 Outbox 做 `json.loads` 丢弃其中 63 次并各自写库推进 `scan_seq`；`reserve` 同事务内隔 47 行（L1205、L1252）重复鉴权两次；只要有 1 个慢回调未返，`tick()` 每 `250ms` 醒来空扫主库，且每 5s 持 `_tick_lock` 串行跑 6 个恢复事务阻塞派发。
- **保持租约围栏的改造方案**：建立复合索引 `mcp_event_outbox(room_id, name, target_grant_id, seq)` 与 `(conversation_id, name, target_grant_id, seq)`，在 SQL 层直接下推 `e.name = s.name AND (e.target_grant_id = '' OR e.target_grant_id = s.grant_id)` 并单条批量 SQL 跳过无关订阅 `scan_seq`；合并 `reserve` 同事务鉴权；将 6 个恢复巡检拆入独立 `reconcile` 协程，空队列时按精确 `_budget_retry_at` 或 `wake.set()` 边沿休眠（零 250ms 空转）。

### OPT-27 [P0] · 修复 `ChannelRouter.for_messages` 124 条静默截断与协作室百级 N+1 查询风暴

- **源码证据**：`hub/collaboration/channel_router.py:782-839`、`chatroom.py:150-400`、`dot_relay.py:143-379`、`projection_cache.py:32-52`、`experience_projection.py:196-211`。
  - `hub/collaboration/dot_relay.py:220` 会把同批派生回复 ID 合并后传入渠道投影。`ChannelRouter.for_messages` 和微信实现都截取前 124 个。隔离 SQLite 中第 124、125 条均为 `rejected`，整批仅返回前者，单查第 125 条能看到 `rejected`。缺失的是已存在的渠道状态。
  - `ChatroomService.views()` 与权限投影仍有逐行查询，缓存键生成还会遍历传入参数。用真实消息组成、SQL 计数和命中率决定批量预取的范围。
- 首批对已完成可见性过滤的 ID 去重、分块查询并完整合并。覆盖多 dot、跨块重复 ID、隐藏消息、失败分项与单条查询的等价性。后续优化保持请求内授权边界，分别计量列表 SQL 次数和投影耗时。

### OPT-28 [P0] · `agent/chat_worker.py` 流式 50ms/16KB 微批合并与 $O(1)$ 配额核算

- **源码证据**：`agent/chat_worker.py:1099-1110, 1218-1312`、`shared/native_cli.py:34-35, 130-137`、`agent/native_cli.py:863-878`、`agent/claude_protocol.py:391-397`。
  - `native.sqlite3` 开启 `synchronous=FULL` + `secure_delete=ON`，`chat_worker.emit` 每收到 1 个流式 Token（50–150 Hz）就执行一次 `INSERT INTO output` + `UPDATE sessions` + 物理 `fsync`，单场长对话制造十万条百字节碎片行；每个运行中 worker **每秒心跳调用 `SELECT COALESCE(sum(length(data)),0) FROM output`** 全表扫描数 GB B-Tree 页（而 `sessions.size` 早已精确维护了字节数！）；空闲会话以 `25ms`（40Hz）轮询 `commands`；点击审批 `chat_answer` 在 `BEGIN IMMEDIATE` 内从 `offset=0` 重放全会话历史；`ClaudeProtocol` 字符串 `+=` 拼接呈 $O(N^2)$。
- **严守不重放边界的改造方案**：对 `delta`/`reasoning` 帧引入 `50ms / 16KB` 内存微批缓冲，遇任何控制/终态/工具/审批帧立即同步刷盘（命令认领 `claimed` 仍先落盘再写管道，偏移 `size` 字节级连续不变，`fsync` 直降 85%–95%）；`retained_bytes` 改为 `SUM(sessions.size)` $O(1)$ 求和；新增轻量 `pending_approvals(session, request_id)` 主键表使审批校验微秒级完成；`ClaudeProtocol` 改用 `list[str]` 收集后单次 `"".join`。

### OPT-29 [P0] · 原生会话的游标查询、逐帧授权与同步恢复

- **源码证据**：`hub/native_cli.py:38-59, 96-174, 225-312, 873-1094`、`shared/native_cli.py:22-38`、`agent/native_sync_flow.py:239-266`。
  - `shared/native_cli.py:49` 已有 `(session, offset)` 主键。`hub/native_cli.py:898` 的导出查询只有 offset 上界，起点以 `offset + length(data) > cursor` 筛选。使用源码表定义及原 SQL，在 SQLite 3.50.4 中放入 16,384 个变长块，近末尾取 4 块要执行 131,095 条 SQLite VM 指令。候选查询先以复合索引定位不晚于游标的最近块，再限定 offset 下界并检查块末尾，相同 4 块只用 82 条指令。
  - 五个游标位置的返回内容逐字节相同。首块为 72 对 103 条指令，中段块内为 65,599 对 82，近末尾块边界与块内均为 131,095 对 82，末端空页为 131,088 对 43。这里测的是数据库工作量，端到端耗时需另测。
  - `hub/native_cli.py:950, 1050` 的 JSON 导出与 SSE 还会逐事件授权。原生同步有变化时会递增服务级 revision、清空整份事件缓存并唤醒订阅者。按会话通知内容变更时，保留独立的撤权和会话删除唤醒。
  - **丢块误 ACK 致 1s 断流**：同步队列满 4 时 `_coalesce(last, data)` 丢弃 `last["chunks"]` 却将 `last_seqs` 放进 `native_ack`，Agent `SyncFlow.on_ack` 发现被丢弃块 `offset` 未推进即误判无进展触发 `1s -> 2s -> 4s` 指数退避停顿。
  - **全局 64 槽争用与失忆**：单一内存 `pending` 字典全局限 64 槽、15s 超时即丢 future 无持久化对账。
- 先独立交付历史游标定位，覆盖起点、块内、块边界、末端和恢复后的缺口。再测连接复用与有界批次授权，保证会话过期、撤销或清除后及时停止释放内容。另对 `_coalesce` 的 ACK 与 `pending` 超时分别测试，核对连续偏移和原控制请求的回执。

### OPT-30 [P1] · 渠道出站 `after_commit` 精确唤醒、两段式文本发送与受管准入一次加密

- **源码证据**：`hub/collaboration/lifecycle.py:42-191`、`channel_router.py:109-367, 666-766`、`weixin_delivery.py:1278-1308`、`weixin.py:1308-1323`、`telegram.py:867-903`、`coordination.py:1450-1509`。
  - 微信/Telegram 回复入队时未触发 `_channel_wakes`，靠 1s 盲轮询（`WeixinDelivery.tick` 每秒跑 `order.backfill`）；发一条纯文本穿越 4 次 `store.run` 与 3 次 `validate`；入站 `gather(4)` 受单慢账号队头阻塞且 Telegram 每轮调 `getMe`；`CoordinationService.admit` 刚调 `_admit_operation` 加密入库后立即解密塞入 `bounded_execution` 再重加密连打 4 条 `UPDATE`。
- **无损改造方案**：在出站入队事务注册 `store.after_commit -> loops.wake_channel(name)`，空队列按 `MIN(next_attempt)` 动态休眠；纯文本镜像合并为 `claim_and_begin + finish` 两段式（保留崩溃置 `unknown` 不自动重发铁律）；入站改为滑动 4 槽并发池并按 `(account_id, credential_version)` 缓存 `getMe` 300s；`_admit_operation` 支持内部 `envelope_patch` 一次构造明文加密落盘。

### OPT-31 [P1/P2] · 跨平台桌面环境/进程树闭环、免深拷贝媒体通道与百 KB 巨型模块拆解

- **源码证据**：`agent/computer.py:86-209, 905-960`、`computer_appserver.py:25-60`、`computer_process.py:25-30`、`native_windows.py:52-90`、`shared/computer_media.py:61-215`、`hub/computer_approvals.py:120-129`；以及 `coordination.py`(103KB)、`events.py`(90KB)、`delegation.py`(72KB)、`chat_worker.py`(62KB)、`collaboration_contracts.py`(55KB)。
  - `agent/computer.py:180-196` 的桌面启动白名单已包含 `SYSTEMROOT` 和 `SystemRoot`。Linux Wayland 会话环境、Windows 用户环境和桌面子进程回收仍需对应平台验证。原生 CLI 发现、媒体对象复制与 WebSocket 重连后的审批恢复分别做验收。
- **无损改造方案**：抽取跨平台最小非敏感 GUI 环境白名单并回退受控 PATH `codex`；将 Win32 `ProcessJob`（`KILL_ON_JOB_CLOSE`）下沉至 `owned_process_group.py` 统一监管桌面子进程树，补齐 `claude` shim；`mcp_result` 改为外层浅拷贝 + 只读引用大字符串，Agent 重连后幂等重推有效期内待决 `computer_approval`；仿照 `experience_projection.py` 与 `claude_protocol.py` 将五大百 KB 单体按纯状态机函数（`pi_protocol.py`、`codex_protocol.py`、`event_matching.py`、`contracts_*.py` 显式重导出）正交拆分，保持外部导入名与幂等指纹逐字节兼容。

---

## 6. 维度五：Web 前端架构、静态产物体积、类型契约与 CI 回归工程效能

### OPT-32 [P0] · `changes-v1.html` 剥离 Zod v4 冗余打包（636 KB → ~20 KB）并修复统一目录工具路由

- **源码证据**：`web/mcp-apps/changes-v1.html:34`（`636,323 B`）、`web/mcp-apps/build.mjs:12-29`、`web/mcp-apps/app.js:1, 48`、`web/mcp-apps/review.js:24`、`hub/mcp_apps.py:29-93`、`hub/mcp.py:694`。
  - 自有业务代码合计仅 `17,347 B`（占 `2.73%`），因引入 `@modelcontextprotocol/ext-apps@2.0.4` 传递拉入 `zod@4.6.6` 命名空间重导出使 esbuild Tree-Shaking 失效，打入 ~600 KB 校验器与 Schema 编译器；且 `review.js:24` / `app.js:48` 硬编码调用旧工具名 `read` 与 `process`，在默认仅公开 `query/act/get_profile` 的统一目录下被严格宿主拦截，同时静态资源设 `ttlMs: 0` 禁缓存且每次请求同步读盘。
- **严守零外联沙箱 CSP 的改造方案**：以约 110 行（压缩后 `<2.5 KB`）零依赖原生 `postMessage` JSON-RPC 2.0 轻量协议桥 `host-bridge.mjs` 替代重型 SDK 打包，对内联 CSS 开启 esbuild minify，将单文件压至 **~20 KB（-96.5%）**；在 `attach()` 绑定字典注入 `catalog_mode`，前端优先路由至 `query(action="changes")` 与 `query(action="job", view="wait")`（旧连接回退 `read/process`）；`hub/mcp_apps.py` 按 `(st_mtime_ns, st_size)` 内存缓存静态 HTML 并设合理 `ttlMs`。

### OPT-33 [P0] · 按路由拆分首屏静态资源，并保留脚本依赖顺序

- **源码证据**：`web/index.source.html:9-62`、`web/public-assets.json:1-12`、`web/lazy-assets.js:4-37`、`scripts/build_web_assets.py:79-97`。
  - 从入口解析出 35 个 JS、16 个 CSS 和 1 个图标，共 52 个不同的静态引用，原始文件合计 1,282,240 字节，未计 HTML。其中 33 个 JS 标有 `defer`。真实网络请求数、传输字节及阻塞耗时受缓存和反向代理影响。`web/lazy-assets.js:31-35` 目前按顺序等待每个按需资源。
- 按真实路由依赖拆分资源，先并行下载可独立的项，为有依赖的经典脚本保留执行次序和失败重试。继续校验哈希和 CSP，分别测试冷启动、命中缓存、切换路由与重试后的交互。

### OPT-34 [P0] · `ReleaseAssets` 启动期确定性内存 GZip 预压缩与构建产物紧凑化

- **源码证据**：`hub/static_assets.py:86-127`、`web/mcp-apps/build-core.mjs:9`、`web/mcp-apps/build-room.mjs:21-26, 46-63`。
  - `build-core.mjs` 设 `minify: false`，`build-room.mjs` 打包后反调 `prettier.format()` 将 `collaboration-room.js` 展开至 `283,835 B`；`ReleaseAssets` 全程无 GZip 协商，跨公网/Tailscale 每次发版重传 `1.69 MB` 明文。
- **100% 兼容发布指纹的改造方案**：`ReleaseAssets.__init__` 先对磁盘原文件严格校验 `assets-manifest.json` 的原始字节数与 `sha256`（磁盘包与升级器契约零改动），随后用 Python 标准库 `gzip.compress(raw, compresslevel=9, mtime=0)` 一次性生成确定性内存预压缩块（全站内存增量 `<350 KB`），仅对静态白名单资产响应 `Content-Encoding: gzip` + `Vary: Accept-Encoding`（动态 `/api`、`/oauth` 不压缩，彻底免疫 BREACH 侧信道）；`build-core`/`build-room` 开启 `minifyWhitespace` + `minifySyntax`。

### OPT-35 [P0] · 补齐 CI 脱管漏跑的 2 套 `.mjs` 单测、纯单元模块 16 合 1 微批执行与同步浏览器复用

- 相关入口位于 `.github/workflows/ci.yml:68`、`.github/workflows/pull-request.yml:54`、`scripts/regression_plan.py:66-128`、`scripts/check_full_regression.py` 和 `tests/conftest.py`。
  - PR 和主 CI 的显式 Node 测试步骤只列出 `test_lazy_assets.mjs` 和 `test_diff_html.mjs`。另两份 `tests/peripheral_ux.test.mjs` 有 6 项，`tests/test_weixin_ui_helpers.mjs` 有 4 项。本次独立运行 **10 passed，0 skipped**。两种命名均应进入常规发现。
  - **660+ 进程冷启动**：`regression_plan.py` 对 400+ 个百毫秒级纯单元模块逐文件启动独立 Python 进程并生成独立 `.coverage` SQLite 库；纯同步 UI 测试也未开启 `conftest.py` 已实现的 `--chat-browser-reuse`。
- **保持强证据链的改造方案**：将 CI 与 PR 工作流统一改为 `node --test tests/*.mjs web/browser-extension/*.test.mjs` 并在仓库布局测试中断言零遗漏；在 `regression_plan.py` 对非 `exclusive`/非 `limited` 的纯单元模块按确定性字典序每 16 个聚合成一个微批 Job（完整保留逐 `nodeid` 的 `collection_sha256` 与精确一次通过校验，省去 ~370 次进程冷启动）；AST 识别无协程的纯同步浏览器模块自动追加 `--chat-browser-reuse`。

### OPT-36 [P1] · 修复 `workflows` 页整树销毁失焦、`dom.mjs` $O(N)$ 桶调和与流式/协作室增量更新

- **源码证据**：`web/app.js:906-925, 1117-1152`、`web/core/dom.mjs:14-36`、`web/chat/transport.js:279`、`web/chat/timeline.js:114-126, 268-276`、`web/collaboration-room/timeline.mjs:379-406`。
  - `app.js:910` 订阅了 `workflows` 的 SSE 自动刷新，但 L1117 的 `CP.dom.replacePage` 白名单漏写了 `'workflows'`，导致每次更新直接落入 `$('#page').innerHTML = html` 整树销毁失焦；`dom.mjs:26` 对无 `data-cp-key` 节点做 `old.find` 呈 $O(N^2)$。
  - `ChatUI.dirty` 在每个流式 Token 写入却在 `chatFlush()` 中从未读取即清空，60Hz 每帧全扫 100 条消息并尝试 `JSON.parse` 未完结工具流；协作室 `patchMessages()` 在 200 条消息循环内跑两遍 `[...list.children].find`（最高 8 万次扫描）并对整条消息做 `JSON.stringify`。
- **无损改造方案**：将 `'workflows'` 补入 `replacePage` 白名单；`dom.mjs` 按 `nodeType:tagName` 预建队列游标将无 Key 匹配降至严格 $O(N)$；消费 `ChatUI.dirty` 仅局部更新已挂载脏项并对 `delta` 态跳过 JSON 美化；协作室预建 `Map(messageId -> Element)` 并以轻量版本元组签名替代整对象 `JSON.stringify`。

### OPT-37 [P1] · `InvocationInspector` 对 `node --check` 安全短路与 Mypy 核心契约扩面

- **源码证据**：`shared/execution_policy.py:166, 229-299, 721-824`、`pyproject.toml:29-41`、`ruff.toml:13-22`。
  - `execution_policy.py:_interpreter()` 未像 `python -m` 那样识别 Node 纯语法检查参数 `--check`/`-c`，导致 `node --check web/app.js`（`app.js` 含 >25,000 个 AST 节点）被 Python 栈逐节点遍历扣光 `self.remaining = 20000` 预算直接报 403 `EXECUTION_POLICY_LIMIT`（正是 `CODE_AUDIT_REMEDIATION_2026-11-04.md` 第 57 行悬而未决问题的根源！）。
  - 全仓 441 个非测试 Python 模块仅 9 个（`2.04%`）纳入 `[tool.mypy]`，且全局 `follow_imports = "silent"` 将跨模块导入静默退化为 `Any`；Ruff 漏开 `F5`/`F63`/`F841`/`PLE`。
- **无损改造方案**：在 `_interpreter()` 照常递归审计完所有 `-r/--require/--import/--loader` 预加载模块钩子后，若为 `javascript` 且含 `--check`/`-c`（无 `-e/--eval/-p/--print`）则安全短路返回，并用 `tree_sitter.Query` 替代全节点栈弹出；分批将 `shared/*.py`、`hub/principal.py`、`hub/db_worker.py`、`hub/api/*.py` 纳入 Mypy 并切至全局 `follow_imports = "normal"`（仅对未迁移旧模块设精确 `overrides`），Ruff 补齐 `["F5", "F63", "F841", "PLE", "RUF100"]`。

### OPT-38 [P2] · CSS 级联倒置门禁化、`Dockerfile` 消除双倍源码层与 CI 锁哈希补齐

- **源码证据**：`web/workspace.css:285-400, 460-795, 803-844`、`scripts/css_override_report.py:86-117`、`Dockerfile:21-31`、`.dockerignore`、`.github/workflows/ci.yml:53, 410`。
  - `workspace.css:803-844` 在文件尾部重复定义基础 `.workspace-project-actions`，跨过前序 `@media (max-width: ...)` 反向覆盖移动端布局，而 `css_override_report.py` 未接入 CI 且不检测同文件自覆盖。
  - `Dockerfile:30` 在 `COPY` 全部目录后执行 `RUN chmod -R a=rX,u+w`，在 OCI 分层文件系统中触发整层 Copy-up 使源码与前端层体积翻倍，且 `.dockerignore` 未排除 `web/readme/`、`web/collaboration-room/*.mjs` 等非运行期构建源；`ci.yml:410` 的 `oidc-authentik` 漏配 `cache: pip` 与 `--require-hashes`。
- **无损改造方案**：升级 `css_override_report.py` 检测同文件重复选择器与 `@media` 后置基础规则并接入 `test_ui_design_guard.py`；`Dockerfile` 引入轻量 `AS source-stage` 完成剔除非运行期前端源与 `chmod -R` 归一化，最终 `runtime` 阶段单条 `COPY --from=source-stage` 落盘（100% 保持 `umask 077` 升级兼容且消除重复层）；补齐 `oidc-authentik` 的 `--require-hashes` 并删去 `ci.yml:53` 重复的 `build-room.mjs --check`。

---

## 7. 维度六：调用方可诊断性、运维韧性与工程熵（第二轮复审新增 OPT-42 至 OPT-52）

> **复审口径**　第二轮以当前项目工作区为准：HEAD `1629d925` 加上 129 个未提交改动，即 OPT-01 至 OPT-41 的实施（见 `docs/FIRST_PRINCIPLES_OPTIMIZATION_2026-11-18_STATUS.md`）。这样可以确认新增项既不与已有条目重复，也没有被现有实施覆盖。本节行号均对应当前工作区。前五个维度关注单次调用的字节、锁和进程开销；本节补上此前未覆盖的四类约束：调用方能否判断等待原因；扫描是否只触及与任务相关的字节；每个进程都要支付的启动成本；服务在重启、备份和长事务下的可用性，以及代码和文档规模带来的维护成本。

### 7.1 调用方可诊断性（每个返回字节都应支撑下一步决策）

#### OPT-42 [P1] · 轮询回执补上当前阶段，合并三份导航信息

- **现场样本。** 本次复审对整个项目发起一次 `query(action="tree")`，总耗时 83.8 s。`view=trace` 显示：Hub 投递 5.19 s 后 Agent 接收，随即进入 `waiting_resource`（`lane=read`、`resource_kinds=["path"]`、`blocked_count=1`），按 Agent 单调时钟等待 73.4 s 后才开始执行，执行与落盘合计不到 0.2 s。等待期间，第 39.1 s 与第 67.2 s 的两次 `job wait` 都只返回 `state="queued"`，没有阶段、排队原因或等待时长。调用方因此无法区分资源冲突、设备离线和投递重试。
- **字节构成。** 按紧凑 JSON 重新编码，这份等待回执为 1,423 B。其中 `next`（148 B）、`next_call`（230 B）与 `recovery`（362 B）共 740 B，占 52%，三者指向同一个后续调用；操作 ID 出现 5 次。真正支撑决策的字段（ID、状态、阶段、建议间隔、输出游标）约 145 B。拼装位置见 `hub/runtime.py:842-850, 1486-1492` 与 `hub/core_tools.py:395`。本连接的 `mcp_view_version` 为 2，紧凑视图下的比例另测。
- **方案。** `get`/`wait` 回执附上最近一次 Agent 阶段的摘要：`stage`、`queue_reason`、`blocked_count`、`stage_age_ms`。这些字段 trace 中已有，直接复用即可，不新增授权面。`next` 与 `next_call` 合并为一个规范调用；`recovery` 只在类别变化或到达终态时完整给出，其余轮询只给 `category`。`?view=2` 的完整回退保留。验收看两点：同一等待操作连续轮询的累计字节；模型能否仅凭回执在取消、改用更窄路径、继续等待三者之间作出选择。

#### OPT-43 [P1] · 资源冲突给出可操作的阻塞描述，并复核整树读取的锁粒度

- 上例中，Agent 侧 `scheduler._blockers()`（`agent/scheduler.py:209-249`）已经算出阻塞者，但 Hub trace 中 `blocked_by=[]`，同一时段本账号的 `job list` 也只列出自己的操作。阻塞者对调用方既不可见，也无法取消。
- `agent/resource_queue.py:376` 把 `fs_tree` 与 `fs_read`、`fs_search` 同归为路径读取。以项目根为路径的 tree 会声明根路径，从而与项目内任一路径上的写入或独占声明冲突。`scan_scope=git_index` 的 tree 只列出索引条目和大小，不承诺内容稳定。它是否需要与内容读取相同的 `read_stable` 语义，应对照 `docs/ADAPTIVE_SCHEDULER.md` 重新确认。
- **方案。** 对调用方无权查看的阻塞者，返回脱敏描述：操作种类（写入、命令、工作树）、是否为同一身份、已持有时长、调用方能否取消，不暴露 ID、路径和参数。新增可选参数 `max_queue_ms`：超时后不产生任何副作用，直接返回 `RESOURCE_BUSY` 及上述描述，由模型决定改用窄路径还是继续等待。对基于 Git 索引的整树列表，评估两种做法：按分页条目声明路径，或采用 `relaxed` 语义并在结果中标注快照性质。验收：并发写入下根目录 tree 的等待 P95；阻塞描述的授权边界测试；现有 `read_stable` 用例保持通过。

### 7.2 工作区扫描边界（只处理与任务相关的字节）

#### OPT-44 [P1] · 统一四套排除规则，并把验收产物移出项目根

- **现状。** 本机项目目录共 16 GB，而 Git 只跟踪 1,530 个文件。已被 `.gitignore` 忽略的 `.work/` 占 11 GB、119,834 个文件、457 个顶层条目，其中约 230 个超过 7 天未修改，最大的单个目录 `merge-branches-20261109` 有 1.2 GB。另有 `dist/` 409 MB、`.venv` 308 MB。`scripts/benchmark_hub_agent.py:805` 与 `scripts/benchmark_mcp_workflow.py:196` 要求输出写到 `.work` 下，但仓库没有对应的保留期限。
- **规则不一致。** 同一个项目里至少有四套排除规则。一是 `.gitignore`。二是 `agent/filesystem.py:40` 的 `IGNORED_DIRS`，含 `node_modules`、`.venv` 等，但不含 `.work`、`dist`、`.codepier-test-runs`。三是 `agent/snapshots.py:92`，只排除 `.git` 与 `.codepier`。四是 baseline 采集自带的列表，排除了 `.work`、`dist/build` 和 `.codepier-test-runs`。结果是：非 Git 范围的 tree/search 会把 `MAX_SCAN_ENTRIES = 100000` 的预算耗在 `.work` 上，快照也可能把 11 GB 验收产物一并复制。本次在项目根执行一次只排除 `.git`、`dist`、`node_modules` 的 `grep -r`，用时 55.3 s；同类查询限定在 `hub`、`agent`、`shared` 内时不到 0.5 s。
- **方案。** 抽出一份共享排除策略，至少涵盖 Git 忽略项、生成物和凭据目录。tree、search、知识地图、快照与 baseline 都引用这份策略，各自只能在其上进一步收紧，不再各写一份。验收产物默认写到 Agent 状态目录或 `$XDG_CACHE_HOME`，按天数和总容量回收；若必须留在项目内，就在 `.work/` 下写入清理清单，交给 OPT-40 的后台维护统一回收。验收：在本项目根执行 tree、search 与快照时的扫描条目数；确需扫描被忽略文件时，显式指定的路径仍能正常工作。

### 7.3 进程启动固定成本（每个进程都要支付的串行段）

#### OPT-45 [P1] · `shared.contracts` 在导入期推导两次输出 schema

- `shared/core_contracts.py:629` 与 `shared/query_contracts.py:49` 在模块导入时各调用一次 `build_core_output_schemas`，后者是前者加上查询工具的超集。在 cProfile 下导入 `shared.contracts` 用时 0.599 s，其中 0.528 s（88%）花在这两次推导上，包括 20 次 `factor` 和 204,487 次 `deepcopy`。不开 profiler 时，`shared.contracts` 自身导入用时 156–165 ms；`import hub.app` 共 580 ms，加载 769 个模块；`import agent.runner` 304 ms。
- 读取、符号与网关校验的工作子进程不导入该模块，不受影响。受影响的是 Hub 与 Agent 的每次启动和升级，以及测试进程：669 个测试文件即使按 OPT-35 每 16 个一组，仍有数十个进程各重算一遍。OPT-05 处理的是每次调用时的重复归约，导入期的这部分成本不在其中。
- **方案。** 先只推导一次超集，再从中切出核心子集，并改为结构共享，去掉防御性深拷贝。然后评估在构建期生成冻结的 schema 文件：由 `scripts/check_repository_contracts.py` 校验它与源码推导结果的 SHA-256 一致，运行时只做加载。验收：两种方式产出的 schema 逐字节一致；`shared.contracts` 导入时间降到 40 ms 以内；同时记录 Hub 从冷启动到 `/healthz` 就绪的时间。

### 7.4 运维韧性（可用性 = 故障间隔 /（故障间隔 + 恢复时间））

#### OPT-46 [P1] · 存活探针不经过单写队列，公开的健康信息降到最小

- `hub/api/system.py:33` 的 `/healthz` 使用默认的 `@database_endpoint(store)`，即 `read_only=False`，探针要排进单写 Worker 才能执行 `SELECT 1`。`deploy/k3s/deployment.yaml:173-193` 用同一路径同时做启动、就绪和存活探针，其中存活探针每 30 s 一次、超时 4 s、连续 3 次失败即重启；`Dockerfile:42` 的 `HEALTHCHECK` 也走这条路径。写 Worker 连续阻塞一分半左右，kubelet 就会把繁忙但健康的 Hub 判为失活并重启。部署是单副本加 `Recreate`，重启就是整站中断，还会切断正在提交的事务和全部 Agent 连接（见 OPT-49），压力因此被进一步放大。
- 该端点无需认证，响应里带有版本号、面板升级状态和写错误计数。
- **方案。** 拆成 `/livez` 与 `/readyz`。`/livez` 只检查事件循环与投递 Worker 协程是否存活，不访问数据库。`/readyz` 走 `run_read`，设短超时，并参考 `store.stats_snapshot()` 中的写队列深度。公开响应只保留 `status`，版本与诊断信息移到需要认证的接口。`/healthz` 保留为兼容别名，三个部署入口同步更新。验收：人为让写 Worker 持锁 120 s，Pod 不被重启，就绪状态按预期摘除后恢复。

#### OPT-47 [P1] · 备份把密钥和密文分开存放，并补上校验与恢复

- `hub/__main__.py:29-52` 的 `_write_backup` 把 `hub.sqlite3`、`master.key` 和 `master.keys.json` 写进同一个 zip。拿到备份文件就能解开全部 Fernet 字段，静态加密对离站备份不起作用。备份副本也不执行 `PRAGMA integrity_check`，而 `hub/keyring.py:212` 的换钥路径会执行。命令行没有 `restore` 子命令；`docs/DEPLOYMENT.md:166-171` 只给出一次性手工备份的步骤，没有定时、保留期和恢复演练。
- **方案。** 默认把密钥单独导出，或用操作者提供的口令或公钥（例如 age recipient）加密归档，并要求密钥与数据库分开存放。发布备份前，对副本执行 `integrity_check` 和现有的 `check-compatibility`，并写出清单（版本、schema、SHA-256、行数概要）。新增 `hub restore --verify`：恢复到临时目录，以只读模式启动并做冒烟检查。另提供可选的定时备份与保留策略，并在文档中写明 RPO/RTO。验收：只拿到数据库文件时无法解密；损坏的副本拒绝发布；从备份恢复后，OAuth 授权、协作房间和附件都能读取。

#### OPT-48 [P2] · 显式管理 Hub WAL 的体积与检查点

- Hub 只设置了 `journal_mode=WAL`、`foreign_keys` 以及新库的 `auto_vacuum=INCREMENTAL`（`hub/store.py:22-23, 107-113`），没有设置 `journal_size_limit` 和 `wal_autocheckpoint`，运行期也不主动执行 `wal_checkpoint`。整个仓库只有测试和 `scripts/panel_update_runtime.py:391` 执行过 `TRUNCATE` 检查点。
- 读池有 3 个连接，长读事务会推迟检查点；`journal_size_limit` 默认为 -1，WAL 文件在峰值之后不会缩回。按 OPT-11 的估算，导入一个 512 MiB 文件约产生 930 MiB WAL 写入，这部分空间峰值过后会一直占着。以上是依据默认配置的推断，需要现场数据确认。
- **方案。** 先在 `stats_snapshot()` 中加入 WAL 字节数、检查点结果（busy、log、checkpointed）和最长读事务时长，采集现场数据。再据此设置 `journal_size_limit`，后台维护做 `PASSIVE` 检查点，空闲时做 `TRUNCATE`。`synchronous` 级别关系到确认前的持久化语义，本项只测量，不作调整。

#### OPT-49 [P2] · Hub 重启后分散 Agent 重连

- `deploy/k3s/deployment.yaml:107-110` 为单副本加 `Recreate`，`terminationGracePeriodSeconds: 20`；`hub/__main__.py:122` 设置 `timeout_graceful_shutdown=10`。在 `agent/runner.py:2668-2688` 中，曾经连接成功的 Agent 会把重连延迟重置为 1 s，再叠加 `U(0,1)` 抖动。于是 Hub 重启后，原本在线的 Agent 会在 1–2 s 的窗口里同时重连，握手、journal 重放与原生会话续传一起压到单写线程上。
- **方案。** Hub 停机前向 Agent 发送 `going_away`，附带按在线数量放大的重连窗口；Agent 改用 decorrelated jitter；Hub 对握手设令牌桶准入。验收：模拟 N 个 Agent，测重启后第一分钟的写队列深度、握手失败率，以及全部 Agent 恢复在线所需的时间。

#### OPT-50 [P2] · Windows 原生会话空闲时的 100 Hz 轮询

- `agent/native_windows.py:266-281` 的读取线程以非阻塞方式读 ConPTY，无数据时 `sleep(0.01)`；`agent/native_windows.py:412` 的命令循环每 10 ms 查询一次本地 SQLite 的 `commands` 表。每个空闲的 Windows 原生会话因此持续以 100 Hz 唤醒两个循环，每秒约查询数据库 100 次。OPT-28 处理的 40 Hz 轮询位于 POSIX 的 `chat_worker`，Windows 这条路径没有覆盖。
- **方案。** 读取改用 overlapped I/O，或在阻塞读的线程里用事件通知；命令表改为由写入方提交后通过命名事件或管道字节唤醒，同时保留一个低频兜底扫描。验收：在 Windows 实机上测空闲会话的 CPU 占用与每秒唤醒次数，并确认输入回显延迟没有变差。

### 7.5 工程熵（变更成本与理解成本）

#### OPT-51 [P2] · 拆分函数级控制流巨石，并用基线棘轮防止反弹

- 当前工作区 `hub`、`agent`、`shared` 共有 4,323 个函数，其中 123 个超过 150 行，19 个超过 300 行。`hub/mcp.py:60` 的 `make_router` 有 775 行，内嵌的 `handle` 就有 689 行，约 184 个分支节点。其余较大的有：`hub/native_cli.py:701` 的 `make_native_router`（567 行）、`agent/runner.py:989` 的 `handle`（466 行）、`agent/runner.py:1687` 的 `execute`（397 行），以及 `shared/core_output_schemas.py:238` 的 `factor`（395 行，141 个分支）。
- 按文件看，`hub/runtime.py` 有 150 KB，`hub/collaboration/rooms.py` 137 KB，`agent/runner.py` 128 KB，`room_runtime.py` 与 `service.py` 各约 101 KB，这些都不在 OPT-31 列出的五个模块里。
- **方案。** 把 `mcp.handle` 改成按 JSON-RPC 方法分派的表，鉴权、预算与呈现抽成可单测的纯函数；`runner.handle` 与 `execute` 按准入、资源、执行、落盘四个阶段拆开。在 Ruff 中启用 `C901` 与 `PLR0915`，以当前数值为基线文件，只允许下降。拆分遵守 OPT-31 的约束：外部导入名与幂等指纹保持不变。

#### OPT-52 [P2] · 降低文档熵：路径引用改为符号锚点，并接入 CI

- `docs/` 下有 141 份被跟踪的文档，共 2,527,645 B，其中 61 份文件名带日期；`CHANGELOG.md` 有 118,802 B。139 份 Markdown 里共有 1,161 处反引号路径引用，其中 39 处指向已不存在的文件，例如 `web/collaboration-relay.js`（6 处）、`web/collaboration-workspace.js`（5 处）、`hub/collaboration/retention.py`（3 处）。另有 372 处引用带行号，代码一改就会悄悄失效。本文自身就有 213 处行号引用，而工作区已经改动了 `hub/runtime.py`（新增 139 行）、`agent/runner.py`（新增 131 行）、`hub/mcp.py`（新增 61 行）等文件，部分行号已经偏移。
- 对一个主要由 Agent 阅读的项目来说，文档就是模型的上下文。失效路径会直接导致读错文件，或多出一轮往返。
- **方案。** 文档引用改为 `path::Symbol`，必要时注明提交号；在 `scripts/check_repository_contracts.py` 中校验路径与符号是否存在。带日期的审计稿收尾后移入 `docs/archive/`，另维护一份 `docs/INDEX.md`，列出当前有效的文档及其状态。`CHANGELOG.md` 按次版本拆分。验收：CI 遇到失效路径直接报错，现有 39 处一次清零。

---

## 8. 六大架构安全红线守恒核对清单

对以上 52 项分别验收权限、恢复及混合版本兼容性。下表列出涉及的现有机制和实现约束。

| 安全/一致性红线 | 仓库现有锚点 | 优化方案守恒机制 |
| :--- | :--- | :--- |
| **实时权限重验（零跨请求授权缓存）** | `hub/principal.py` · `hub/store.py` · `agent/runner.py` | `security_epoch` 仅消除非安全写入触发的只读重试；SSE 路由提示仅用于跳过无关空间；`ReadWorker` 热池与 LSP 温池每次借出仍跑完整 `root` 与实时权限重验。 |
| **CAS 并发写防线与写前备份** | `expected_sha256` · `agent/filesystem.py` · `batch_patch.py` | 搜索返回的 `sha256` 来自同源 `read_bytes`；`PatchChange(action="edit")` 全量校验 `expected_sha256` 并在写前完成单次去重备份与原子回滚。 |
| **持久幂等与未知副作用绝不重放** | `idempotency_key` · `agent/journal.py` · `weixin_delivery.py` | 锁外预加密不改变数据库内事务级幂等查重与插入原子性；外部渠道两段式合并仍保留发送中断置 `unknown` 屏障；取消通知不伪造已投递写操作的回滚结论。 |
| **进程组/Job 所有权与无孤儿残留** | `agent/owned_process_group.py` · `native_windows.py` | `pidfd_open`/`kqueue` 保持 `WNOWAIT` 钉住僵尸组长直至整组终止才最终 `waitpid`；Windows 桌面 Provider 统一纳入 `KILL_ON_JOB_CLOSE` Job Object。 |
| **混合版本与 Wire Epoch 兼容** | `shared/tool_protocol.py` · `hub/mcp.py` | 新参数（`context_lines`、`PatchChange.edit`、快照 Overlay）均为可选增量；旧 Agent 自动按缺省行为或明确能力码拒绝，不静默降级；保留 `?view=2` 完整回退。 |
| **严格 CSP、零外联沙箱与资产清单校验** | `hub/http.py` · `hub/mcp_apps.py` · `static_assets.py` | 路由拆包沿用 `type="application/json"` 数据块；`changes-v1.html` 保持 100% 内联零外联；`ReleaseAssets` 先校验磁盘原始 `sha256` 再生成 `mtime=0` 内存 GZip。 |

第二轮新增项的附加约束：OPT-42、43 的阶段摘要与阻塞描述只暴露种类、身份是否相同和时长，不泄露其他操作的 ID、路径或参数；`max_queue_ms` 超时前不产生任何副作用。OPT-44 的共享排除策略只能收紧扫描范围，不能放宽保护路径和凭据目录。OPT-46 的 `/livez` 不报告就绪状态，`/readyz` 不改变现有鉴权。OPT-47 把密钥分离后，恢复流程仍须能在离线状态下完成。

---

## 9. 分阶段无回归实施路线图

### Phase 1　先修可见错误，并给慢请求分段计时
1. **附件和渠道回执。** 先交付房间上传 8 MiB 边界与中断释放（OPT-11），以及跨 124 条批次的渠道状态合并（OPT-27）。两项分别从实际 HTTP 入口和 dot 回复投影验收。
2. **投递与历史会话。** 并行推进接收确认分段计时和 journal 磁盘通道（OPT-21），以及已有索引上的游标定位（OPT-29）。每项独立比较，跟踪同一操作的终态、恢复和副作用。
3. **用量与测试。** 补齐批读可见文本计量（OPT-08）和两个文件的 10 项 Node 用例（OPT-35）。登录、设置、错误恢复等客户端行为独立验收。
4. **可用性与数据安全。** 拆分健康探针（OPT-46），备份密钥分离、校验与恢复（OPT-47）。两项改动范围小，直接决定出事故时能否恢复，所以与可见错误同批处理。

### Phase 2　减少重复读取、维护开销与共享队列等待
1. **Hub 读取与会话。** 量化读重试和写锁占用，在权限依赖验证完备后做读池分流（OPT-09）。评估锁外序列化和加密（OPT-10）、会话级缓存失效与 ACK 对账（OPT-29）、定向通知和提交后唤醒（OPT-26、OPT-30）。
2. **Agent 读取与调度。** 先推进固定版本文件分页（OPT-39）、按项目后台回收运行目录（OPT-40）和数据帧最低发送进度（OPT-41）。以同一负载对照后，再扩展搜索附带上下文和 SHA、局部补丁、备份去重、读进程复用和退出通知（OPT-02、03、17 至 20）。
3. **协议、前端与 CI。** 先测默认与紧凑视图兼容性、知识上下文需求和静态 schema 构造成本（OPT-01、04 至 07）。按路由测前端拆包、压缩、渲染与焦点保持（OPT-32 至 36），并分别验证静态检查和纯单元测试分组（OPT-35、37）。
4. **调用方可诊断性与扫描边界。** 轮询回执的阶段摘要与导航合并（OPT-42），阻塞描述与等待上限（OPT-43），共享排除策略与验收产物外置（OPT-44），导入期 schema 单次推导（OPT-45）。OPT-42、43 以本文 7.1 节的现场操作为回归样本。

### Phase 3　用专项对照验证跨平台、存储和长期维护方案
1. **快照、容器与脱敏闭环**：升级 `SnapshotManager` 目录级 `clonefile` / `FICLONE` ioctl、重写 `.venv` 自引用 shebang 并增加 ext4 Overlay 后端（`OPT-22`）；构建 Aho-Corasick + 行/PEM 两级流式脱敏（`OPT-23`）；实现 `container_v1` 封闭硬链接计数与三元预检、VPS 本机 `OperationLog` 与远端 `CODEPIER_RUN_DIR`、以及符号/LSP 温池（`OPT-24`）。
2. **CAS 存储与在线运维**：落地 `incoming_files` 临时块与 P16 附件磁盘分块流式 AEAD CAS + DEK 信封密钥（`OPT-11`、`OPT-12`）；升级 Gateway 常驻校验进程池（`OPT-15`）；实现三阶段零停机密钥轮换与 `incremental_vacuum`（`OPT-16`）。
3. **跨平台桌面、模块解耦与 CI 提速**：补齐 Linux/Windows 桌面 GUI 环境白名单与 Win32 `ProcessJob` 监管（`OPT-31`）；推进纯单元模块 16 合 1 微批回归与同步浏览器复用（`OPT-35`）；分批将 `shared/*` 与 `hub/api/*` 纳入 `normal` Mypy，优化 `Dockerfile` 多阶段准备消除重复镜像层，并将五大百 KB 单体模块按纯函数状态机正交拆解（`OPT-31`、`OPT-37`、`OPT-38`）。
4. **其余专项。** 产物分块、SSE、进程控制、协作投影、流式落盘与密钥维护（OPT-12 至 16、25、28）分别保留容量、撤权、故障恢复和混合版本验证。
5. **运维与工程熵。** WAL 观测与检查点（OPT-48），重启时分散重连（OPT-49），Windows 会话事件唤醒（OPT-50），函数复杂度棘轮（OPT-51），文档锚点与归档（OPT-52）。OPT-48 至 50 先采集现场或实机数据，再决定参数。

---

## 10. 配套基准与回归验证入口

每批次改造合入前，按以下既有工具链独立验证并分别报告源码测试、构建清单与基准对照结果：

- **MCP 上下文与协议基准**：`python -m scripts.benchmark_mcp_workflow`、`pytest tests/test_mcp_usage.py tests/test_unified_tools*.py`
- **Hub / Agent 并发与队列争用基准**：`python -m scripts.benchmark_hub_agent`、`python -m scripts.benchmark_gateway`
- **前端构建、资产哈希与全部 Node 单测**：`npm --prefix web/mcp-apps run build`、`python scripts/build_web_assets.py --check`、`node --test tests/*.mjs web/browser-extension/*.test.mjs`
- **仓库契约、静态检查与源码清单校验**：`ruff check .`、`mypy`、`python scripts/check_repository_contracts.py`、`python scripts/update_source_inventory.py --check`

### 10.1 同一提交的实测记录与验收口径

- 对照审阅基线为 `1629d925355b1f011ae1f541d339df44b893ca8f`。现场投递记录见 OPT-21，SQLite VM 指令见 OPT-29，应用层读取量见 OPT-39，目录扫描与帧次序见 OPT-40、41。各项计数使用各自的单位。生产 P95 和吞吐需要相同数据与负载下的前后对照。
- 已运行 `pytest -q tests/test_http_body_admission.py tests/test_room_attachments.py tests/test_transport_priority_and_backpressure.py`，37 项通过。另行运行 `node --test tests/peripheral_ux.test.mjs tests/test_weixin_ui_helpers.mjs`，10 项通过。既有测试结果与复审的边界探针分别记录，前者不能代替修复后的完整入口回归。
- 首批变更按用例记录附件临界值、单条与批量回执一致性、确认前 journal 提交、断线恢复及长会话分页结果。全量 CI、跨平台实机和生产负载属于后续实施验收。

### 10.2 第二轮复审（OPT-42 至 OPT-52）的取证记录

- **口径。** 在项目节点（macOS，Python 3.13.12）的当前工作区只读执行，未改动任何源码。HEAD 为 `1629d925`，另有 129 个未提交改动。
- **现场操作。** OPT-42、43 的数据取自同一个 `tree` 操作的 `job wait` 回执与 `view=trace`；字节数按紧凑 JSON 重新编码后统计。
- **静态统计。** 用 AST 统计 `hub`、`agent`、`shared` 的函数行数与分支节点（OPT-51）；用正则统计 `docs/` 下的反引号路径引用，并逐一核对文件是否存在、行号是否越界（OPT-52）；用 `du`、`find` 统计工作区目录（OPT-44）。
- **导入成本。** `python -X importtime` 与 cProfile 分别测量 `hub.app`、`agent.runner`、`shared.contracts` 以及各工作子进程入口（OPT-45）。单次测量受页缓存影响，作为数量级依据。
- **尚未验证。** OPT-46 的探针误杀、OPT-48 的 WAL 增长、OPT-49 的重连集中和 OPT-50 的空闲 CPU 依据源码与部署清单推断，尚未在 k3s 集群、生产 Hub 或 Windows 实机上复现，实施前需先补测量。
