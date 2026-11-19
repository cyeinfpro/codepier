# CodePier (`MCP`) 智能体 MCP 调用链路第一性原理深度诊断与根治方案（2026-11-19）

> **审计基线**：分支 `main` · HEAD `d9dfa13b885aa3a3709be26a1718a23d5df154e9`（工作区干净 `dirty=0`）
>
> **覆盖链路**：
> - **Hub MCP 协议、呈现与计量层**：`hub/mcp.py`、`hub/mcp_streams.py`、`hub/core_tools.py`、`hub/unified_tools.py`、`hub/tool_router.py`、`shared/contracts.py`、`shared/collaboration_contracts.py`、`shared/public_collaboration.py`、`shared/mcp_presentation.py`、`shared/token_estimate.py`
> - **Hub 存储、锁竞争与运行时层**：`hub/store.py`、`hub/store_worker.py`、`hub/runtime.py`、`hub/integrations.py`、`hub/connection_evidence.py`、`hub/collaboration/events.py`、`hub/collaboration/lifecycle.py`、`hub/collaboration/monitor.py`、`hub/gateway/remote.py`、`shared/config.py`、`shared/crypto.py`
> - **Agent 执行调度、文件系统与语义层**：`agent/runner.py`、`agent/journal.py`、`agent/scheduler.py`、`agent/filesystem.py`、`agent/workspace_probe.py`、`agent/lsp_navigation.py`、`agent/telemetry.py`、`shared/scheduler_config.py`
> - **回归门禁基线**：`tests/test_bounded_batch_read.py`、`tests/test_mcp_token_presentation.py`、`tests/test_output_fidelity_ranges.py`、`tests/test_mcp_catalog_modes.py`、`tests/test_mcp_output_fidelity.py`、`tests/test_mcp_gateway.py`、`tests/test_mcp_gateway_protocol.py`、`tests/test_resource_semantics_v2.py`、`tests/test_queue_hub_optimizations.py`、`tests/test_hub_adaptive_admission.py`、`tests/test_hub_recovery_and_event_delivery.py`、`tests/test_recovery_integration.py`、`tests/test_connection_evidence.py`、`tests/test_adaptive_agent_integration.py`

---

## 0. 实测复现证据与第一性原理建模

### 0.1 本次在线调用实测复现的五大现场病灶

在本次直接通过 CodePier MCP 调查 `MCP` 仓库的过程中，无需构造极端压测环境，日常智能体读写与并发探测即现场复现出五类典型症状：

1. **轻并发阶梯串行化（卡并发）**
   - **实测现象**：同秒并发发出 4 个轻量只读请求（1 次 `help` + 3 次小文件读），完成耗时呈典型串行阶梯 `2s -> 6s -> 7s -> 9s`；同秒 3 并发呈现 `4s -> 8s -> 11s`；写入本文档前的二次复核中，并发 `project` + `file` + `changes` 同样呈 `2s -> 4s -> 8s` 阶梯排队。
   - **指向根因**：`B1` + `B14`（心跳高频刷新全局 `security_epoch`/`session_revision` 逼退只读并发）、`B3`（单线程 `StoreWorker` + `synchronous=FULL` 串行绞肉机）、`B2`（Agent 事件循环主线程同步抢 `journal.lock` 做 `FULL` fsync）与 `B10`/`B17` 叠加。

2. **有效载荷预算腰斩 50%（浪费 Token + 翻倍分页）**
   - **实测现象**：批量读取 2 个文件，仅返回 `23,766` 字节即触发 `budget_limited: true, payload_limit_bytes: 49152`；单文件读取 `docs/AUDIT_PAGE_FIRST_PRINCIPLES_OPTIMIZATION_2026-11-18.md` 在 `24,431` 字节（139 行）即被强制截断分页。
   - **指向根因**：`B4`（默认 `view="2"` 触发 `mirror="full"`，使 `content[0].text` 与 `structuredContent` 100% 重复输出，48 KiB 总预算被双倍镜像吞噬一半）。

3. **目录分页死胡同（智能体卡死 / 盲区）**
   - **实测现象**：现场两次调用 `query(action="tree", args={"depth": 1, "path": "docs"})` 均返回：
     ```json
     {"truncated": true, "next_offset": null, "scan_scope": "git_index", "omitted": {"docs": 86}}
     ```
     既告知调用方“结果不完整（省略 86 项）”，又不提供 `next_offset`，且任何 `offset` 参数都无法越过 `per_dir=40` 的硬截断，智能体陷入“被截断却无路可翻页”的死胡同。
   - **指向根因**：`B5`（`agent/filesystem.py:708-744`）。

4. **盲盒 `args` 契约税（浪费往返与 Token）**
   - **实测现象**：`query` / `act` 的统一工具 Schema 仅暴露通用 `args: object`，批量读未显式说明分页键名导致误传 `start_line`/`end_line` 触发 `INVALID_ARGUMENTS`，被迫额外发起一轮 `query(action="help", ...)` 往返后再重试，单次读演变为 3 轮 RPC。
   - **指向根因**：契约信息密度失衡 + `B4` 错误回执双写放大。

5. **后台知识维护长期饥饿**
   - **实测现象**：连续多次 `query(action="project", project="MCP")` 均返回：
     ```json
     "knowledge": {"state": "ready", "maintenance": {"state": "deferred_busy", "enabled": true}}
     ```
   - **指向根因**：Hub 单写队列（`B3`/`B17`）与 Agent 通道配额/锁竞争（`B10`/`B2`）共同压榨后台维护窗口。

---

### 0.2 从第一性原理建模：一次 MCP 调用的物理成本下限 vs 当前实际路径

从计算机系统第一性原理出发，**一次无副作用的只读文件查询**（如 `query(action="file", path="...")`）的最小必要物理工作量只有 5 步：

1. **鉴权校验**：基于内存态凭证与单调安全纪元（仅在真正发生权限变更时递增）做 $O(1)$ 校验（`0` 次磁盘写）；
2. **工具路由**：$O(1)$ 哈希表命中 `UNIFIED_TOOLS`（`0` 次深拷贝、`0` 次远程网关查表）；
3. **网络派发**：异步加密并经全双工 WebSocket 发出请求帧（不阻塞事件循环，不与大载荷队列互斥）；
4. **受控读取**：复用热进程/轻量文件读取（`1` 次路径校验 + `1` 次文件读取，`0` 次同步 `FULL` fsync）；
5. **单副本回传**：以紧凑单副本格式回传响应并一次完成预算裁剪与准确 Token 计量。

而当前 CodePier (`MCP`) 实际执行一次最简单的只读文件查询时，真实路径膨胀为一条**横跨 6 处串行瓶颈的阻塞链**：

```text
[智能体 POST /mcp]
  │
  ├─► (1) StoreWorker[1线程] : origin_allowed + public_url + tools.resolve(全量 deepcopy 18个Schema + 查gateway) [B3]
  ├─► (2) refresh_principal 重复跑 3 次；每12s心跳刷 devices.info + last_seen 触发 security_epoch & 4x session_revision [B1+B14]
  │       └─► 并发 run_read 3次重试失效 ─► 第4次强抢 _writer_lock 全局栅栏 或 503 AUTHORIZATION_CHANGED
  ├─► (3) StoreWorker[1线程] : _begin_project_trace (INSERT mcp_activity + fsync) + _admit_operation + online() [B3]
  │       └─► device_dispatch_lock 互斥入队与投递准备 [B12]
  ├─► (4) Hub 事件循环主线程同步 AES-GCM 加密 + 4s硬超时(超时直接击毁整条WS隧道与全部并发在途任务) [B16]
  ├─► (5) Agent 事件循环主线程同步解密 + 同步抢 journal.lock 做 FULL fsync(缺 synchronous=NORMAL) [B2+B18]
  │       └─► ReadPool(4) 与 read_limit(8) 失配 + 硬编码 ("root","root","execute") 跑4次管道RPC + 2次 integrations.project [B10]
  ├─► (6) 回传后 Hub 侧：
  │       ├─► connection_evidence.completed(): Fernet解密 + 4次SELECT + 无节流INSERT (叠加 returned() 共2次写库) [B17]
  │       ├─► integrations.capture_output(): 262KB 逐字符正则+Unicode 挤占唯一写线程 [B3]
  │       ├─► integrations.finish(): UPDATE + 全表扫描 DELETE + usage_metrics.record = 3次 FULL fsync [B3]
  │       └─► core_tools.py:887 + mcp.py:806 连续跑 2 次 bound_batch_response + mirror="full" 输出 200% 双份响应 [B4]
```

由此可抽象出指导本次治理的**四条第一性原理公理**：

| 公理 | 核心陈述 | 当前架构的根本违背 |
| --- | --- | --- |
| **公理 1：状态分层与变更频率隔离** | 高频易失遥测（如 12s 心跳 `last_seen`、调度快照）与低频强一致安全策略（如凭证吊销、角色变更）**物理上不得共享同一版本号与触发器**。 | `devices` 表被纳入 `security_epoch` 与 `_security_write`，且心跳每 12s 无条件写库 2 次，把“设备活着”误等价为“权限已变”（`B1` + `B14`）。 |
| **公理 2：单写临界区最小化** | SQLite 单写线程（`StoreWorker`）与事件循环主线程是全局最稀缺的串行资源；**一切只读查询、纯 CPU 计算、大文本扫描与非关键遥测写必须剥离出关键路径**。 | 纯内存校验、Schema 深拷贝、262KB 正则估算、每 5s 7 次协作扫表、每次调用 3–5 次 `FULL` fsync 全挤在单写线程与事件循环上（`B2`/`B3`/`B16`/`B17`）。 |
| **公理 3：控制面契约闭环自洽** | 面向自主智能体的分页游标、超时策略、取消权限与流式心跳必须**语义诚实且可终止**，严禁输出“截断但无游标”或“假截断诱导死循环”的矛盾状态。 | `tree` `per_dir` 截断无游标（`B5`）、`search` 跳过二进制即报 `truncated=True`（`B13`）、`fail_fast` 遇队列容量退化为无限等（`B6`）、只读不能取消自己的慢读（`B8`）。 |
| **公理 4：信息熵与单副本传输** | 智能体上下文窗口是最高昂的带宽；**协议默认路径必须单副本、高信息熵传输，且计量口径必须严格等于真实发送载荷**。 | 默认 `view=2`/`mirror=full` 双倍重复输出导致 48KB 预算腰斩（`B4`），同时 `estimate_output` 只计 `content` 忽略 `structuredContent` 制造 2x 监控盲区（`B12`）。 |

---

### 0.3 全量 18 项缺陷总览矩阵（B1–B18）

| 编号 | 级别 | 所属维度 | 精确源码定位 | 一句话核心根因 | 治理阶段 |
| --- | :---: | --- | --- | --- | :---: |
| **B1** | **P0** | 卡并发 | `hub/store.py:167-193, 225-293, 580-598`<br>`hub/runtime.py:3344-3355` | `devices.last_seen` 触发 `cp_epoch_devices_UPDATE` 与 `_security_write`，刷新全局 `security_epoch` 与双倍 `session_revision`；`operations` 不在 `_epoch_tables` 致并发读被迫栅栏化或抛 `503 AUTHORIZATION_CHANGED`。 | Wave 1 |
| **B14** | **P0** | 卡并发 | `hub/runtime.py:3275-3342` | `_heartbeat()` 对 5 个字段无条件置 `changed=True`，每 12s 额外执行数 KB `UPDATE devices SET info=?`，与 `_touch_agent()` 叠加成双倍写事务 + 双倍 fsync + 4 次 `session_revision` 自增（**必须与 B1 同步修复**）。 | Wave 1 |
| **B2** | **P0** | 卡并发 | `agent/journal.py:24-32`<br>`agent/runner.py:213-221, 615-638, 1172-1399, 1450-1475`<br>`agent/telemetry.py:96-120` | `agent.sqlite3` 缺 `synchronous=NORMAL` 与 `timeout`；事件循环主线程（含 `scheduler.condition` 内）同步抢 `journal.lock` 做 `FULL` fsync；L1450-1451 连续两次 `telemetry.snapshot`；L1469 写入后 L1471 未清 `"trace:" + id` 多发无效 trace 帧。 | Wave 1 |
| **B3** | **P0** | 卡并发 | `hub/store_worker.py:20-21`<br>`hub/store.py:100-153`<br>`hub/mcp.py:100, 197, 200, 310, 422, 486-487, 518, 645`<br>`hub/tool_router.py:115-121`<br>`hub/integrations.py:329-382`<br>`hub/runtime.py:1714`<br>`shared/contracts.py:1691-1695`<br>`shared/public_collaboration.py:518-539` | 单线程 `StoreWorker(max_workers=1)` + `synchronous=FULL` 承担大量纯读/纯 CPU/高频写：`origin_allowed`/`public_url`、3x `refresh_principal`、`tools.resolve` 全量深拷贝原生 Schema 并动态构建协作 Schema、`capture_output` 262KB 正则、`finish()` 每调用 3 次 fsync 含全表 `DELETE`。 | Wave 1 |
| **B4** | **P0** | 浪费 Token | `hub/mcp.py:69-74, 504-516, 603-614, 806-817`<br>`hub/core_tools.py:442-473, 546-644, 825, 887-898`<br>`shared/mcp_presentation.py:1238-1247` | 默认 `CODEPIER_MCP_DEFAULT_VIEW="2"` 导致 `mirror="full"` 100% 双份响应，48KB 预算腰斩至 ~23KB；`view=2` 缺少 `view=3` 紧凑视图；协作与错误硬编码双写；`bound_batch_response` 连续跑两次。 | Wave 1 |
| **B5** | **P1** | 卡住 | `agent/filesystem.py:708-744` | `tree` 的 `per_dir=40` 在 `offset` 之前截断并剪枝子目录，返回 `truncated: true` 但 `next_offset: null`，形成分页死胡同。 | Wave 2 |
| **B13** | **P1** | 卡住 | `agent/filesystem.py:972, 987` | `search` 只要跳过任意 1 个二进制/超大文件（`skipped_reasons` 非空）即强置 `truncated=True` 并给非空 `next_offset`，诱发智能体白跑全仓重扫；`scan_limited` 下用匹配条数当游标致翻页必空。 | Wave 2 |
| **B6** | **P1** | 卡住 | `agent/scheduler.py:454-466` | `wait_policy="fail_fast"` 仅在 `self._blockers(item)` 非空时抛错；因 `project_limit`/`lane_capacity`/`resource_pressure`/`remote_target_limit` 排队时退化为无限 `await event.wait()`。 | Wave 2 |
| **B7** | **P1** | 卡住 | `agent/scheduler.py:442-453` | `max_queue_ms` 超时后未检查 `if not event.is_set()`，竞态下误杀已获槽并进入 `self.active` 的操作。 | Wave 2 |
| **B8** | **P1** | 卡住 | `hub/runtime.py:1587-1625` | `_cancel()` 无条件要求 `execute` 权限致只读智能体无权取消慢读；`cancellable_reads` 遗漏 `open_workspace`/`project_knowledge_*`/`searches_start`。 | Wave 2 |
| **B9** | **P1** | 卡住 | `hub/mcp.py:456-464`<br>`hub/mcp_streams.py:26, 66-76` | SSE keepalive 与 `notifications/cancelled` 默认不激活且硬要求 `plan is not None`（`core`/`full`/`coding` 彻底不可用）；单凭据硬限 16 流（429）；`events()` 未捕获异常在 HTTP 200 头后炸断流。 | Wave 2 |
| **B10** | **P1** | 卡住 / 并发 | `agent/runner.py:794-798, 922, 1334-1365`<br>`agent/workspace_probe.py:19, 157-161, 264-266`<br>`shared/scheduler_config.py:119, 123`<br>`agent/scheduler.py:179-181` | `strict` 锁阻断全部只读；`fs_search` 在两处归入 `execution` 通道被饿死；`read_limit=8` 与 `ReadPool(4)` 失配反复冷启销毁子进程；单项目硬砍 1 槽；单次读硬编码 `("admission","root","root","execute")` 跑 4 次管道 RPC（含 2 次 `"root"`）；`PROBE_TIMEOUT=15s` 与 `wait=30s` 冲突。 | Wave 2 |
| **B15** | **P1** | 卡住 | `agent/lsp_navigation.py:531-684` | 每次 LSP 语义查询均冷启全新语言服务进程组等待冷索引，并在 `finally: await peer.close()`（L684）单次查完即杀进程。 | Wave 3 |
| **B16** | **P1** | 卡住 / 并发 | `hub/runtime.py:125-147, 3585-3625`<br>`shared/crypto.py:89-123`<br>`agent/runner.py:504` | `Connection.send()` 4s 超时即置 `unusable=True` 关闭整条 WS 隧道击毁所有并发在途任务；事件循环主线程同步加解密 multi-MB 载荷与 JSON 解析；Hub 读循环 `await connection.send(reply)` 被 `connection.lock` 反向锁死。 | Wave 2 |
| **B17** | **P1** | 卡并发 | `hub/collaboration/events.py:1698-1738`<br>`hub/collaboration/lifecycle.py:31-40, 130-135`<br>`hub/collaboration/monitor.py:877-880`<br>`hub/connection_evidence.py:123-176`<br>`hub/runtime.py:3102`<br>`hub/mcp.py:311, 540`<br>`hub/unified_tools.py:172-203, 228, 249`<br>`hub/integrations.py:280-284` | 协作后台高频轮询写队列（`events` 每 5s 连发 7 次、`reconcile` 每 5s 3–4 次、`collector` 每 2s 2 次）；每次读完成做 Fernet 解密 + 4 次 SELECT + 无节流 `INSERT`（叠加 `returned()`/`authentication` 多写）；纯 CPU `open_handle`/`mint_handle` 占写线程；重叠会话循环 N 次独立 `UPDATE mcp_activity`。 | Wave 2 |
| **B11** | **P2** | 卡并发 | `hub/gateway/remote.py:106-156` | 非 `MODERN` 或 stateful 远端 MCP 强制并发=1；`await self.initialize()` 网络握手在持有 `self.condition` 期间执行，阻塞所有等待者与 `close()`。 | Wave 3 |
| **B12** | **P2** | 并发 / 计量 | `hub/unified_tools.py:306-342`<br>`hub/runtime.py:1584, 1698, 2438`<br>`shared/config.py:83, 97`<br>`shared/token_estimate.py:28-65, 158-179, 251-294` | `_wait()` 10s 切碎轮询丢弃 `<1s` 余量；`device_dispatch_lock` 入队/取消与投递互斥；`delivery_rolling_devices` 默认空；`estimate_output` 仅计 `content` 忽略 `structuredContent`（2x 镜像盲区）且排除 `_meta`/Schema/非 batch `files`。 | Wave 1 (计量) / Wave 3 (并发) |
| **B18** | **P2** | 卡并发 | `agent/runner.py:503-595` | Agent WS 接收循环串行 `await disk_call(self.journal.ack)`（L586）与 `await self.report_status()`（L571, L573），造成收包队头阻塞。 | Wave 3 |

---

## 1. 维度一：并发瓶颈与锁竞争（卡并发的时候）

本维度聚焦于“为何智能体同时发起 3–4 个轻量只读调用，耗时就会退化为 `2s -> 6s -> 7s -> 9s` 的阶梯串行，甚至在高并发下触发 503 重试风暴”。

### 1.1 `B1` [P0] `devices` 表心跳误入 `security_epoch` 触发器，并发读被迫抢写锁栅栏

- **源码铁证**：
  1. `hub/store.py:167-193`（`_install_security_epoch`）：正则 `pattern` 将 `devices` 表纳入 `_epoch_tables`，并为每张表安装 `INSERT`/`UPDATE`/`DELETE` 三类触发器 `cp_epoch_devices_UPDATE`，每次更新直接执行 `UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='security_epoch'`；而高频读写的 `operations` 表**不在** `_epoch_tables` 中。
  2. `hub/store.py:580-598`（`_security_write` / `_advance_session_revision`）：只要写 SQL 文本匹配 `\b(?:...|devices|...)\b`，立即执行 `self.session_revision += 1`，同时注册 `self._writer_db.after_commit(self._advance_session_revision)` —— **单次 `UPDATE devices` 会使 `session_revision` 连续自增 2 次**！
  3. `hub/runtime.py:3344-3355`（`_touch_agent`）：每个在线 Agent 每 12 秒执行一次 `UPDATE devices SET last_seen=? WHERE id=?`。
  4. `hub/store.py:225-293`（`_read_phase`）：只读连接池 `run_read()` 每次进入会记录快照前后的 `session_revision` 与 `PRAGMA data_version`。只要读期间有任意一次 `UPDATE devices`（或者读到了不在 `_epoch_tables` 中的 `operations` 表且期间有任意并发写改变了 `data_version`），就会判定 `changed` 或 `data_changed` 为真并重试；连续 3 次失败后，第 4 次（`attempt == 3`）强制 `with self._writer_lock:` **全局阻塞写线程**，若仍冲突则抛出 `503 AUTHORIZATION_CHANGED`（L287-292）。

- **第一性原理根因**：
  `devices` 表混合存储了两类物理属性截然不同的数据：
  - **低频安全控制面字段**：`token_hash`、`revoked`、`space_id`、`owner_user_id`、`deleted`（直接决定连接与项目授权）；
  - **高频易失存活态字段**：`last_seen`、`info` 里的运行态快照（只用于 UI 展示与在线判定，完全不改变任何权限结论）。
  将整张 `devices` 表笼统纳入表级 `AFTER UPDATE` 触发器与正则 `_security_write`，等价于**每 12 秒向全系统广播一次“全局权限已被吊销”，人为摧毁 SQLite WAL 模式的 MVCC 读写并发能力**。更糟的是，由于 `operations` 不在 `_epoch_tables` 中，任何对 `operations` 的常规写入都会让读取 `operations` 的只读请求跳过 `security_epoch` 捷径而反复重试。

- **根治优化设计**：
  1. **表结构冷热剥离（首选）**：将高频易失字段拆分至独立表 `device_presence(device_id PRIMARY KEY, last_seen REAL, runtime_info TEXT, updated_at REAL)`（**不纳入** `_epoch_tables` 与 `_security_write`）；原 `devices` 表仅保留身份、密钥、归属与撤销字段。
  2. **列级触发器防线（低迁移成本过渡方案）**：若需保持单表兼容，SQLite 原生支持 `AFTER UPDATE OF <security_columns> ON devices` 列级触发器；同步将 `_security_write()` 改为基于明确声明的写事务分类（显式 `security_write=True` 标志），而非对任意 SQL 字符串跑表名正则。
  3. **解耦 `operations` 读一致性**：对 `operations` 等数据表的读操作区分“权限判定读（依赖 `security_epoch`）”与“业务状态读（允许基于单事务 WAL 快照读取，不因并发写 `operations` 而重跑 4 次并强抢 `_writer_lock`）”。

---

### 1.2 `B14` [P0] `_heartbeat()` 5 处无条件 `changed=True`，与 `B1` 叠加制造 2x 写事务 / 4x 纪元抖动

- **源码铁证**：
  - `hub/runtime.py:3275-3342`（`_heartbeat`）：
    - L3281-3284：`build` 只要是 `dict` 且 `len <= 12000` 即无条件 `info["build"] = build; changed = True`（未比较 `build != info.get("build")`）；
    - L3285-3292：`device_actions` 校验通过即无条件 `changed = True`（未比较差异）；
    - L3301-3304：`scheduler` 快照非 `None` 即无条件 `changed = True`（而每次心跳 `scheduler` 的瞬时负载、队列深度、时间戳都在变化）；
    - L3305-3308：`scheduler_error` 为空串 `""` 时也无条件 `changed = True`；
    - L3309-3316：`scheduler_revision` 校验通过即无条件 `changed = True`；
    - L3337-3341：最终每 12 秒必执行一次长达数 KB 的 `UPDATE devices SET info=? WHERE id=?`。
  - 与 `hub/runtime.py:3350-3354` 的 `_touch_agent()` 叠加后，**单个在线 Agent 每 12 秒就会对 `devices` 表执行 2 次独立写事务 + 2 次 `FULL` fsync + 2 次 `cp_epoch_devices_UPDATE` 触发 + 4 次 `session_revision` 自增**！若有 10 台在线设备，平均每 1.2 秒就有一次全局只读失效风暴。

- **强依赖警告**：
  > **仅修 `B1` 的 `last_seen` 而不修 `B14`，`devices.info` 每 12 秒的无条件写入依然会持续引爆 `security_epoch` 与单写线程 `FULL` fsync！`B1` 与 `B14` 必须作为原子变更在 Wave 1 捆绑治理。**

- **根治优化设计**：
  1. **严格变更检测（Diff-before-Write）**：对 `build`、`device_actions`、`management`、`scheduler_error`、`scheduler_revision` 均先做值等价比较（`if new_val != info.get(key):`），无变化零写库。
  2. **运行态快照内存化**：`scheduler` 的高频瞬时遥测快照（CPU/内存/实时队列数）直接保存在 `connection.runtime_snapshot` 内存中供 UI/调度读取，**不再持久化写入 `devices.info`**；仅在进程启动或配置 revision 变更时落盘静态元数据。
  3. **合并 `last_seen` 节流持久化**：将 `last_seen` 持久化周期从 12s 放宽至 60s（实时在线状态始终以内存 `connection.last_seen` 为准），并写入拆出的 `device_presence` 表。

---

### 1.3 `B2` [P0] Agent `agent.sqlite3` 缺 `synchronous=NORMAL` 且主事件循环同步抢锁做 `FULL` fsync

- **源码铁证**：
  1. `agent/journal.py:24-32`：
     ```python
     self.db = sqlite3.connect(self.directory / "agent.sqlite3", check_same_thread=False)
     ...
     PRAGMA journal_mode=WAL;
     ```
     未设置 `PRAGMA synchronous=NORMAL;`（SQLite 默认 `FULL`，每次提交强制同步刷盘两次），也未显式指定 `timeout=30`（默认 5s，高负载易抛 `database is locked`）。
  2. `agent/runner.py:213-221, 615-638, 1172-1399`：`phase()`、`mark_running()`、`is_cancelled()`、`status()`、`cancel()` 等方法在 asyncio 事件循环主线程（甚至在持有 `scheduler.condition` 异步锁期间）**直接同步调用**持有 `self.journal.lock` 的 SQLite 读写。一旦后台 `disk_call` 线程正在执行 `journal.finish()` 的 `FULL` fsync（单次在 macOS APFS/机械盘上可达 `15–80ms`），主事件循环即刻被 `RLock` 锁死，全 Agent 所有并发任务的收包、心跳与调度全部冻结！
  3. `agent/runner.py:1450-1451` + `agent/telemetry.py:96-120`：
     ```python
     self.phase(id, "persisting")
     result["trace"] = self.telemetry.snapshot(id)
     ```
     `phase()` 内已调用过一次 `self.telemetry.snapshot(id)`（L219），紧接着 L1451 又立刻再查一次 SQLite 生成相同快照，连续两次触发 `telemetry.snapshot(id)` 与状态序列化开销。
  4. `agent/runner.py:1469-1471`（及 `L1383` 取消路径）：
     ```python
     self.phase(id, "result_ready")
     self.telemetry.starts.pop(id, None)
     self.pending_output.pop(id, None)
     ```
     L1469 刚刚写入了 `self.pending_output["trace:" + id]`，而 L1471 仅清理了 `id`、未同步清理 `"trace:" + id` 键，导致任务已经返回 `result` 后，后台刷新循环仍会额外多发一帧残留的 `trace` 报文，造成 Hub 侧无效状态处理与网络噪音。

- **根治优化设计**：
  1. **SQLite 持久化参数规范化**：`agent/journal.py` 初始化时显式设置 `timeout=30.0`，并在 WAL 模式下执行 `PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=30000; PRAGMA temp_store=MEMORY;`（WAL 模式下 `synchronous=NORMAL` 严格保证进程崩溃原子性与持久化，仅放弃 OS 断电最后一帧，fsync 开销下降 80%+）。
  2. **彻底清除事件循环同步盘 I/O**：将 `mark_running()`、`status()`、`cancel()` 等热路径状态改为内存权威映射（`self.active_states[id]`）+ 异步落盘队列，严禁在持有 `scheduler.condition` 或事件循环主线程内同步抢占 `journal.lock`。
  3. **修复 `trace` 重复快照与键泄漏**：合并 L1450-1451 为单次 `snapshot`；L1471 与 L1383 同步补齐 `self.pending_output.pop("trace:" + id, None)`。

---

### 1.4 `B3` [P0] Hub 单线程 `StoreWorker(max_workers=1)` + `synchronous=FULL` 沦为全局串行绞肉机

- **源码铁证**：
  - `hub/store_worker.py:20-21`：`self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-sqlite")`，整个 Hub 只有一个写工作线程。
  - `hub/store.py:100-153`：仅设 `PRAGMA journal_mode=WAL`，**未设 `PRAGMA synchronous=NORMAL`**，导致每一次 `store.execute()` 都是独立的 `FULL` fsync 事务！
  - 在此之上，单次 `/mcp` 工具调用竟然把 **7 类本不该占用写线程的工作** 全部串行塞进了 `store.run(...)`：
    1. **入口静态配置读占写线程**（`hub/mcp.py:197, 200`）：每次 HTTP 请求都连续 `await runtime.store.run(origin_allowed, request)` 与 `await runtime.store.run(public_url)` —— 两次串行排写队！
    2. **`refresh_principal` 单请求重复 3 次**（`hub/mcp.py:310` + `hub/tool_router.py:30` + `hub/unified_tools.py:259` / `hub/runtime.py` 多处重复刷新同一凭证，且多次走写线程而非只读/内存快照）。
    3. **`tools.resolve` 先全量深拷贝原生 Schema + 动态构建协作 Schema，再判断统一工具**（`hub/tool_router.py:115-121`）：
       ```python
       principal, native, external = self.definitions(principal, discovery=False)
       from shared.unified_contracts import UNIFIED_TOOLS
       if name in UNIFIED_TOOLS:
           return ToolRoute("native", name)
       ```
       即使 99% 的调用都是 `query` 或 `act`，也先触发 `self.definitions()` 全量 `copy.deepcopy` 原生巨型嵌套 JSON Schema（`shared/contracts.py:1691-1695`）、动态执行 `host_parameter_schema()` 构建协作 Schema（`shared/collaboration_contracts.py:648-651` → `shared/public_collaboration.py:518-539`）并查询 `gateway.tools` 表，然后才发现 `name in UNIFIED_TOOLS` 直接丢弃所有构建结果！
    4. **纯只读/非写检查误走写线程**（`hub/mcp.py:100, 310, 422, 486-487, 518` & `hub/runtime.py:1714`）：`_begin_project_trace` 先 `store.run(runtime.project, ...)`（L100），`refresh_principal`（L310）、`tools.resolve`（L422）、`runtime.gateway.context`（L486-487）、`mcp_apps.attach`（L518）与 `runtime.online()`（`hub/runtime.py:1714`）全部误走 `store.run` 而非 `store.run_read` 或内存态读取（另 L311 `observe("authentication")` 与 L540 `connection.returned()` 属于 `B17` 无节流写库）。
    5. **重 CPU 文本扫描挤占写线程**（`hub/mcp.py:644-650`）：
       ```python
       await runtime.store.run(runtime.integrations.capture_output, trace, ...)
       ```
       `capture_output()` 是**纯内存/纯 CPU** 操作（对最高 262 KB 响应文本跑逐字符正则与 Unicode 分类估算 `estimate_output`），与数据库毫无关系，却独占 SQLite 唯一写线程，直接卡死全站所有写事务！
    6. **`integrations.finish()` 单次调用触发 3 次独立 `FULL` fsync 含全表清理**（`hub/integrations.py:338-382`）：
       - 事务 1（L353-361）：`UPDATE mcp_activity SET service_ms=?,status=?,operation_id=? WHERE id=?`（+ fsync）
       - 事务 2（L373-375）：`DELETE FROM mcp_activity WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM mcp_activity)`（每次调用都扫表删旧记录 + fsync！）
       - 事务 3（L380）：`self.usage_metrics.record(trace["id"], trace["token_usage"])`（+ fsync）

- **根治优化设计**：
  1. **Hub SQLite 开启 `PRAGMA synchronous=NORMAL`**（`hub/store.py` 初始化时设置，并提供 `CODEPIER_SQLITE_SYNCHRONOUS=FULL` 环境变量供极端合规场景回退）。
  2. **`tools.resolve` 快速路径前置（保留碰撞与鉴权不变量）**：对 `name in UNIFIED_TOOLS` 走基于 `security_epoch` / `gateway_catalog_revision` 缓存的轻量化名称碰撞校验，彻底跳过 `self.definitions()` 的全量 `deepcopy` 与协作 Schema 重建；并对 `definitions()` 产出的只读 Schema 做冻结缓存（`MappingProxyType` / 不可变副本），消除每次请求的深拷贝。
  3. **彻底分流非写任务**：
     - `origin_allowed`、`public_url` 改为内存配置缓存（仅配置变更时刷新）；
     - `runtime.online(device_id)` 直接读内存 `self.connections`（$O(1)$，零 SQL）；
     - `runtime.project`、`mcp_apps.attach`、`gateway.context` 迁至 `store.run_read`；
     - `capture_output` 迁出 `store.run`，放入独立轻 CPU 线程池或直接轻量计算（严禁碰 `StoreWorker`）。
  4. **单请求 `principal` 复用与 `finish()` 事务合并**：
     - 同一 HTTP 请求周期内以 `security_epoch` 为哨兵复用 `principal`，仅在纪元变化时刷新；
     - `integrations.finish()` 将 `UPDATE mcp_activity` 与 `usage_metrics.record` 合并为**单事务提交**；将 `DELETE FROM mcp_activity` 改为每 500 次写入或每 5 分钟后台低频触发一次，**从每调用 3 次 fsync 降至 1 次（且非关键遥测可走异步批量合并写队列）**。

---

### 1.5 `B17` [P1] 后台协作轮询风暴与单次读双倍 `connection_evidence` 无节流写库

- **源码铁证**：
  1. **协作后台多循环高频轮询写队列**（`hub/collaboration/events.py:1698-1738`、`hub/collaboration/lifecycle.py:31-40, 130-135`、`hub/collaboration/monitor.py:877-880`）：
     即使系统完全空闲无活跃协作任务：
     - `events` 循环（每 2s 调度，`lifecycle.py:35`）每次都会执行 `store.run(self.reserve)`，且每 5s 触发 `_relay_recover_after` 时在 `StoreWorker` 写队列上连续排队 **7 次 `store.run`**（`recover_paused_work`、`recover_continuations`、`dot_relay.recover`、`dot_mailbox.recover`、`room_consumers.recover`、`task_liveness.recover`、`reserve`，`events.py:1698-1738`）；
     - 叠加 `reconcile` 循环（每 5s 调度，`lifecycle.py:31, 130-135`）连续 3–4 次 `store.run`，以及 `collector` 循环（每 2s 调度，`lifecycle.py:40` → `monitor.py:877-880`）每次 2 次 `store.run`（`reserve` + `cleanup`），持续抢占单写线程。
  2. **每次只读完成后额外 Fernet 解密 + 4 次 SELECT + 双倍无节流写库**（`hub/connection_evidence.py:123-176`，由 `hub/runtime.py:3102` 与 `hub/mcp.py:311, 540` 同步调用）：
     - `completed()`（L127-176）对每次成功的 `read` 操作都重新查 `grants`、重新构造 `grant_principal`、重新查 `operation_row`、重新查 `project`、**对 `operation["payload"]` 执行 Fernet 解密与 `json.loads`**，随后无节流 `INSERT INTO mcp_connection_evidence`；
     - 同时 `hub/mcp.py:540` 返回阶段又调用 `returned()`（L123-125）再次 `observe(principal, "call", tool=name)` 无节流写库（加之 `hub/mcp.py:311` 认证阶段 `observe(principal, "authentication")`） —— **一次只读调用硬生生多出 2–3 次 `mcp_connection_evidence` 写事务 + 1 次对称解密 + 4 次冗余查询**！
  3. **纯 CPU 令牌签发占写线程**（`hub/unified_tools.py:172-203, 228, 249`）：`open_handle`（L228）与 `mint_handle`（L249）均为纯加解密与 JSON 编解码计算（零 SQL），却被包装在 `runtime.store.run(...)` 中挤占写队列。
  4. **重叠会话循环 N 次独立 `UPDATE`**（`hub/integrations.py:280-284`）：并发 N 个请求重叠时，对同会话活跃记录循环逐条执行 `UPDATE mcp_activity`，形成 $O(N^2)$ 写放大。

- **根治优化设计**：
  1. **协作后台合并与空闲退避**：将 7 次串行 `store.run` 合并为**单次只读探测（`store.run_read`）**，仅当确实存在到期/待恢复记录时才触发单事务写恢复；空闲期退避至 15–30s（由事件写入显式 `wake.set()` 即时唤醒）。
  2. **`connection_evidence` 内存去重节流 + 零解密**：证据记录的语义是“该凭证在某项目/工具上存在有效契约调用”，同一 `(grant_id, project_id, kind)` 在内存 LRU 中保留 60s 去重窗口，窗口内跳过重复 `INSERT`；`completed()` 直接复用 `runtime` 内存已验证的上下文，消除 Fernet 二次解密与 4 次回查 SQL。
  3. **剥离纯 CPU 与批量化重叠更新**：`open_handle`/`mint_handle` 移出 `StoreWorker`；重叠会话改用单条 `UPDATE mcp_activity SET transition='overlap' WHERE id IN (...)`。

---

### 1.6 `B18` [P2] Agent WS 接收循环串行持久化造成收包队头阻塞

- **源码铁证**：`agent/runner.py:503-595` 在 `async for packet in socket:` 单协程收包主循环中，遇到 `kind == "ack"` 时直接 `await self.disk_call(self.journal.ack, data["id"])`（L586），遇到 `kind == "probe"` 或重复 `call` 时直接 `await self.report_status(...)`（L571, L573）。
- **第一性原理根因**：单条 WebSocket 连接上复用了新任务下发（`call`）、紧急取消（`cancel`）、心跳配置（`scheduler_config`）与低优先级确认（`ack`）。在收包循环中 `await` 磁盘写入会把后续所有 `call` 与 `cancel` 帧堵在 TCP 接收缓冲内。
- **根治优化设计**：`ack` 入队至有界异步持久化器（`asyncio.Queue` + 批量 `UPDATE calls SET acked=1 WHERE id IN (...)`）；`probe` 通过 `create_task` 异步回包，保证 WS 接收循环保持非阻塞 $O(1)$ 分发。

---

### 1.7 `B11` [P2] 远端 MCP 网关并发退化与持锁握手阻塞

- **源码铁证**：`hub/gateway/remote.py:106-156`：
  - L111-114：只要远端 MCP 协议 `self.version != MODERN` 或 `not self.stateless`，`_ready()` 强制将并发压至 `1`（即使下游本身支持 JSON-RPC ID 多路复用）。
  - L130-131：`if not self.active: await self.initialize()` 在持有 `async with self.condition:` 锁期间执行跨公网 TCP/TLS/MCP 握手！握手慢或超时期间，所有等待协程与优雅关闭 `close()` 全部被锁死。
- **根治优化设计**：引入 `initializing: bool` 状态位，持有 `self.condition` 仅做状态转换，**释放锁后再执行 `await self.initialize()`**，完成后重新加锁广播 `notify_all()`；允许为支持并发的远端连接器配置显式 `concurrency` 上限。

---

### 1.8 `B12`（并发相关部分）[P2] `_wait()` 切碎轮询、`device_dispatch_lock` 过度互斥与滚动投递默认关闭

- **源码铁证**：
  1. `hub/unified_tools.py:306-342`：`_wait()` 每次最多只等 `min(10, int(remaining))` 秒（L319），把 `wait=30` 硬切碎为 3 轮独立的 `runtime.invoke("process", ...)`（额外触发 3 轮认证、追踪与查库），且 `if remaining < 1: break`（L316）直接丢弃 `<1s` 的剩余时间窗。
  2. `hub/runtime.py:1584, 1698, 2438`：`device_dispatch_lock(device_id)` 同时包住“取消（L1584）”、“新操作入队（`_admit_operation`，L1698）”与“投递准备（`_prepare_for_send`，L2438）”，并发请求在锁外空等。
  3. `shared/config.py:83, 97`：`delivery_rolling_devices` 默认为空元组 `()`（通过 `HUB_DELIVERY_ROLLING_DEVICES` 环境变量控制），默认关闭了滚动并发投递优化。
- **根治优化设计**：`_wait()` 改为直接监听 `runtime.futures[operation_id]` 或单次长等待（由 `wait_cancelled` Event 立即中断，零轮询切碎）；缩小 `device_dispatch_lock` 临界区仅覆盖发送序列号保护，将 `_admit_operation` 与投递解耦；压测验证后默认启用滚动投递。

---

## 2. 维度二：智能体卡住、假死与死胡同（卡住的地方）

本维度聚焦于“智能体在真实编排中为何会陷入翻页死循环、无声挂死、取消失败、超时连坐雪崩以及语义导航秒级卡顿”。

### 2.1 `B5` [P1] `tree` 分页死胡同：`per_dir=40` 截断并剪枝却返回 `next_offset: null`

- **源码铁证**：`agent/filesystem.py:708-744`：
  ```python
  parent = rel.rpartition("/")[0] or "."
  shown[parent] = shown.get(parent, 0) + 1
  if shown[parent] > per_dir:
      omitted[parent] = omitted.get(parent, 0) + 1
      if stat.S_ISDIR(st.st_mode):
          pruned.add(rel)
      continue
  index += 1
  if index <= args["offset"]:
      continue
  ...
  result = {
      "entries": rows,
      "truncated": bool(more or scan_error or omitted),
      "next_offset": args["offset"] + len(rows) if (more or walk_limited) and rows else None,
      ...
  }
  ```
- **现场复现证据**：本次对 `MCP` 项目执行 `query(action="tree", args={"depth": 1, "path": "docs"})`，直接返回 `"truncated": true, "next_offset": null, "omitted": {"docs": 86}` —— `docs/` 下超过 40 个文件之后的 86 个文档**通过 `tree` 永久不可达**！因为 `shown[parent] > per_dir` 发生在 `index <= args["offset"]` 之前，哪怕调用方强行传 `offset=40`，前 40 项计入后被 `offset` 跳过，第 41–126 项继续被 `shown[parent] > 40` 丢弃，最终返回空数组 `[]`。
- **根治优化设计**：
  1. **语义拆分**：`depth == 1`（明确单目录列举）时，**取消 `per_dir=40` 限制**，完全受 `offset`/`limit` 分页控制，让单目录分页严格闭环；
  2. **多级树显式区分字段**：`depth > 1` 启用目录宽度保护时，将 `per_dir_capped` 与 `page_truncated` 拆分为两个正交字段（避免混淆分页截断与宽度折叠），并提供可配置的 `per_dir` 参数及明确引导提示 `next_hint`（例如“对被折叠目录单独调用 `depth=1` 分页查看”）。

---

### 2.2 `B13` [P1] `search` 误报 `truncated=True` 诱发全仓空转重扫 + `scan_limited` 游标失效

- **源码铁证**：`agent/filesystem.py:972, 987`：
  ```python
  truncated = truncated or candidates_capped or bool(skipped_reasons)  # L972
  ...
  "next_offset": args["offset"] + len(results) if truncated and results else None,  # L987
  ```
- **双重逻辑 Bug**：
  1. **假截断诱发白跑一整轮全仓重扫**：任何包含 1 张图片、1 个锁文件或 1 个 >1 MiB 文件的仓库，`skipped_reasons` 必然非空（如 `{"binary": 1}`）。L972 强行置 `truncated = True`，L987 只要本页有任意匹配项就吐出非空 `next_offset`。循规蹈矩的智能体见 `truncated=True && next_offset!=null` 立即按 `next_offset` 翻下一页，**重头全仓再扫一遍只为换回 `matches: []`**，白白浪费 1 轮完整搜索时间与 Token！
  2. **`scan_limited` 下游标语义错位**：当触发 32 MiB / 8s 扫描上限（`scan_limited`）时，`next_offset` 返回的是**已命中条数** `offset + len(results)` 而非**已扫描文件位置游标**。下一页重头从第 0 个文件开始扫，必然在完全相同的位置再次撞上 32 MiB / 8s 上限，跳过前面相同的命中条数后永远返回空数组 `[]`！
- **根治优化设计**：
  1. **严格区分“分页未完”与“部分文件跳过”**：`next_offset` 仅在 `page_limited`（命中数达到 `limit`）时生成；`skipped_reasons` 仅作为元信息保留，**绝不置位 `truncated` 或生成 `next_offset`**。
  2. **基于文件序号/路径的续扫游标**：当触发 `scan_limited` 时，返回 `resume_after_path`（最后完成扫描的有序相对路径），下一次调用从该路径之后继续扫描，彻底消除死循环重头撞墙。

---

### 2.3 `B6` [P1] `wait_policy="fail_fast"` 在容量排队时退化为无限挂起

- **源码铁证**：`agent/scheduler.py:454-466`：
  ```python
  elif wait_policy == "fail_fast":
      try:
          await asyncio.wait_for(event.wait(), initial_wait_seconds)
      except TimeoutError:
          async with self.condition:
              if not event.is_set() and self._blockers(item):
                  raise DevError("RESOURCE_BUSY", ..., 409, blocked_by=self._blockers(item))
  # 否则落入下方的无限 await event.wait()！
  ```
- **第一性原理根因**：`self._blockers(item)` 仅返回**资源锁冲突**的阻塞者。当任务是因为 `project_limit`、`lane_capacity`、`resource_pressure`（内存暂停）或 `remote_target_limit` 排队时，`_blockers(item)` 为空列表 `[]`！于是 `fail_fast` 分支悄无声息地穿透，退化为无超时的无限 `await event.wait()`，智能体明明声明“拿不到立即失败以便换路”，却被死死挂住直到 HTTP 超时。
- **根治优化设计**：在 `fail_fast` 超时分支中，只要 `not event.is_set()` 即立刻抛出 `RESOURCE_BUSY`（或精确原因码如 `QUEUE_CAPACITY_BUSY`），并在 `details` 中如实附带 `reason = self._reason(item)` 与 `blocked_by = self._blockers(item)`，绝不允许静默穿透到无限等待。

---

### 2.4 `B7` [P1] `max_queue_ms` 超时竞态误杀已获槽激活的操作

- **源码铁证**：`agent/scheduler.py:442-453`：
  ```python
  if max_queue_ms is not None:
      try:
          await asyncio.wait_for(event.wait(), max_queue_ms / 1000)
      except TimeoutError:
          async with self.condition:
              blockers = self._blockers(item)
          raise DevError("RESOURCE_BUSY", "超过最大资源等待时间，未开始执行", 409, blocked_by=blockers) from None
  ```
- **第一性原理根因**：与 L459 的 `fail_fast` 不同，`max_queue_ms` 捕获 `TimeoutError` 后重新获取 `self.condition` 时**漏写了 `if not event.is_set():`**！在 `asyncio.wait_for` 超时回调与 `_drain_all()` 放行交错的同一事件循环 tick 内，调度器可能已经将 `item` 移入 `self.active` 并置位 `event.set()`，结果外层却无条件抛出“未开始执行”，误杀已获槽操作并造成资源释放清理路径竞态。
- **根治优化设计**：在 `async with self.condition:` 内首先检查 `if event.is_set(): pass`（允许已获槽操作继续执行），仅当 `not event.is_set()` 时从等待队列移除并抛出 `RESOURCE_BUSY`。

---

### 2.5 `B8` [P1] 只读智能体无权取消自己的慢读，且 3 类慢读未进可取消白名单

- **源码铁证**：`hub/runtime.py:1587-1625`（`_cancel`）：
  1. L1591：`if op["project_id"]: self.authorize(principal, "execute", project_id=op["project_id"])` —— 无论被取消的任务是 `exec` 还是纯只读查询，一概硬性要求 `execute` 权限！拥有只读 profile（`read` scope）的智能体一旦发出耗时 30s 的全仓搜索或大目录扫描，连**取消自己发起的只读任务**都会被 `403 INSUFFICIENT_SCOPE` 拒之门外。
  2. L1600-1607：`cancellable_reads = {"read", "fs_tree", "fs_read", "fs_read_many", "fs_search", "project_context"}` 遗漏了同样可能慢查的 `open_workspace`、`project_knowledge_query`、`project_knowledge_refresh` 与 `searches_start`，投递后取消直接报 `NOT_CANCELLABLE`。
- **根治优化设计**：
  1. 若目标操作属于只读工具集合且发起者为当前 `principal` 本人（同 `user_id`/`grant_id`），仅要求 `read` 权限即可取消；跨主体取消或写/执行类操作保持 `execute` 权限校验。
  2. 将 `cancellable_reads` 统一从单一权威只读工具表派生，补齐 `open_workspace`、`project_knowledge_*` 与 `searches_start`。

---

### 2.6 `B9` [P1] SSE 保活与取消通道默认休眠、硬限 16 流且异常裸断流

- **源码铁证**：
  1. `hub/mcp.py:456-464`：
     ```python
     if (
         plan is not None
         and plan.wait
         and ("progressToken" in metadata or request.query_params.get("stream") == "1" and plan.wait > 10)
     ):
         stream_ready.set_result(streams.begin(request, principal, identifier))
     ```
     - 默认既无 `progressToken` 也无 `?stream=1`，长等待（如 `wait=30`）下反向代理（Nginx/Cloudflare）可能因空闲无字节回传而直接 504 切断连接；
     - 硬性条件 `plan is not None` 导致 `core`、`full`、`coding` 三个非 unified profile **彻底无法激活 SSE keepalive 与 `notifications/cancelled` 取消通道**！
  2. `hub/mcp_streams.py:26`：单凭据硬限 16 条流（`>= 16` 抛 `429 TOO_MANY_STREAMS`），多子智能体共享同一凭据并发工作时极易撞墙。
  3. `hub/mcp_streams.py:66-76`：`events()` 异步生成器内 `response = work.result()` 未包 `try/except`，一旦任务在流启动后抛出未捕获异常，HTTP `200 text/event-stream` 响应头已经发出，连接直接粗暴截断，客户端既收不到 JSON-RPC 错误帧也不知道任务是否落库。
- **根治优化设计**：
  1. 将 SSE 保活触发条件解耦出 `plan is not None`，对所有 profile 只要预期等待超过阈值（如 `> 5s`）且客户端 `Accept` 包含 `text/event-stream` 即自动启用轻量 keepalive；
  2. 在 `events()` 内部捕获 `work.result()` 异常并转为标准 JSON-RPC error SSE 帧（`event: message\ndata: {...}\n\n`）后优雅结束流；
  3. 单凭据流上限可配置化（默认提高至 64，并与全局上限联动做按需动态回收）。

---

### 2.7 `B10` [P1] Agent 调度与读隔离层六重内耗与饥饿

- **源码铁证**：
  1. **默认 `strict` 锁让任意 `exec` 阻断全项目所有只读**（`shared/scheduler_config.py:119, 123`）：默认 `resource_semantics="strict"` 下，任何未显式声明只读的 `exec`（如跑 `pytest`、构建、甚至 `git log`）都会持有项目根目录独占写锁，导致同一智能体并发读取项目内任意无关源码文件全部挂死排队！
  2. **`fs_search` 在两处被划入 `execution` 通道惨遭饿死**（`agent/runner.py:794-798` 与 `_read_admission_context` 的 `L922`）：
     ```python
     lane = "read" if metadata or tool in BOUNDED_FILE_TOOLS and tool != "fs_search" else "execution"
     ```
     当项目有长命令运行或触发 `memory_paused`（`execution` 通道缩容/暂停）时，智能体连最基本的代码搜索都会被堵死。
  3. **`read_limit=8` 与 `ReadPool(limit=4)` 失配引发子进程冷启抖动**（`agent/workspace_probe.py:157-161`，`limit=4` 在 L160）：调度器允许 8 个只读并发进入执行态，但 `ReadPool` 仅缓存 4 个常驻子进程；第 5–7 个并发读每次都只能现 `spawn` 新 Python 解释器子进程并在用完后立即销毁，平添数百毫秒冷启动抖动与 CPU 峰值。
  4. **单项目硬砍 1 槽**（`agent/scheduler.py:179-181`）：`project_cap = min(..., max(1, limit - 1))`，在单项目开发场景下即便其他项目完全空闲，也强行闲置 1 个槽位；当 `limit=2` 时单项目直接退化为串行 `1`。
  5. **单次读固定跑 2 次 `_operation_root`（`admission=True` 时共 4 次管道 RPC）+ 2 次 `integrations.project`**（`agent/workspace_probe.py:264-266` & `agent/runner.py:1334-1365`）：
     ```python
     phases = (("admission",) if self.admission else ()) + ("root", "root", "execute")
     ```
     协议层甚至把重复执行两次 `"root"` 写死成了合法状态机（L1334-1340 与 L1354-1361 连续两次执行 `integrations.project` 与 `_operation_root`）！
  6. **超时参数倒挂**（`agent/workspace_probe.py:19`）：`PROBE_TIMEOUT = 15` 秒，而 MCP 工具默认 `wait = 30` 秒；复杂批量读或慢磁盘下，子进程在 15s 被强杀报 `PROJECT_DISCOVERY_TIMEOUT`，远早于外层等待预算。

- **根治优化设计**：
  1. 清理 `agent/runner.py` 重复的 `_operation_root` 与 `integrations.project` 调用，将 `workspace_probe` 阶段收敛为 `("root", "execute")`（或单次合并 RPC），减少 50% 进程间往返；
  2. 将 `ReadPool.limit` 与调度器 `read_limit` 绑定为同一配置源（消除 8 vs 4 失配），并将 `PROBE_TIMEOUT` 按操作剩余截止时间动态传递（上限对齐 30s）；
  3. `fs_search` 迁回独立 `search` 或 `read` 通道（配合单操作 8s/32MiB 上限保护），不再被长 `exec` 命令饿死；
  4. 单项目借用逻辑改为**仅当确实存在其他项目在 `self.waiting` 等待时才保留 1 槽**，无竞争时允许跑满全部容量；
  5. 为常见只读/构建类 `exec` 或显式 `read_only=true` 提供非独占共享模式，降低 `strict` 根锁误伤。

---

### 2.8 `B15` [P1] LSP 语义查询每次冷启语言服务、查完即杀

- **源码铁证**：`agent/lsp_navigation.py:531-684`：
  每次调用 `query(action="symbols")`（定义、引用、调用层级）都会拉起全新的语言服务进程组（如 `pyright`/`typescript-language-server`/`rust-analyzer`），从零加载整个工程类型图与符号索引，查完单个符号后在 L684：
  ```python
  finally:
      await peer.close()
  ```
  立即将刚刚辛苦建立索引的语言服务进程彻底杀掉！
- **第一性原理根因**：语言服务器的物理工作模式是**高启动成本（数秒~数十秒、数百 MB 索引）+ 极低热查询成本（5–50ms）**。按单次 RPC 生命周期管理 LSP 进程，等于把 5ms 的热查询强行退化为 5–20s 的冷索引重启，不仅让智能体极易触发 `LSP_TIMEOUT`，还把宿主机 CPU 打满。
- **根治优化设计**：
  引入按 `(project_root, language)` 索引的**受控 LSP 常驻池（`LspPool`）**：
  - 保留空闲 TTL（默认 5 分钟）与全局上限（如最多 2–3 个常驻实例，LRU 淘汰 + 内存压力回调优先回收）；
  - 通过文件变更事件（`workspace/didChangeWatchedFiles` 或基于 `source_sha256` 触发 `didOpen`/`didChange`）增量同步文档，严守 `source_current` 一致性契约；
  - 进程异常崩溃时自动清理重建，对外热查询延迟从秒级降至 `<100ms`。

---

### 2.9 `B16` [P1] WS 隧道 4s 脆断连坐雪崩 + 主线程同步加解密 + 读写互锁

- **源码铁证**：
  1. **4s 硬超时直接击毁整条隧道与全部在途并发任务**（`hub/runtime.py:125-147`）：
     ```python
     async with self.lock:
         ...
         try:
             await asyncio.wait_for(self.socket.send_text(self.channel.pack(body)), 4)
         except BaseException:
             self.unusable = True
             with contextlib.suppress(Exception):
                 await asyncio.wait_for(self.socket.close(code=1011), 1)
             raise
     ```
     只要网络抖动、大载荷发送或对端事件循环因 `B2` 卡顿超过 4 秒，Hub 立即标记整条 Agent WS 连接 `unusable=True` 并以 `1011` 强关 —— **同一 Agent 上正在并发执行的其他 10 个健康任务全部连坐中断重连**！
  2. **事件循环主线程同步加解密 multi-MB 载荷**（`shared/crypto.py:89-123`、`hub/runtime.py:3586`、`agent/runner.py:504`）：单帧最大支持 `16 MiB`，`json.dumps` + AES-GCM 加密 + Base64 编解码全在 Hub/Agent 主事件循环线程同步执行，单次大包可直接冻住事件循环数十至上百毫秒，反向诱发 4s 超时连环炸断。
  3. **Hub 读循环被写队列反向锁死**（`hub/runtime.py:3610, 3624`）：WS 收包循环在收到帧后直接 `await connection.send(reply)`，需要去抢 `connection.lock`；如果写端正排着大包发送队列，**收包循环就被写锁卡死无法读出 Agent 发来的心跳与结果帧**，进一步触发 45s 读超时断连！

- **根治优化设计**：
  1. **读写彻底解耦**：为每条 `Connection` 引入独立有界发送协程（`send_queue`，区分高优控制帧如 `ack`/`heartbeat`/`cancel` 与普通数据帧），WS 收包循环仅 `put_nowait` 入队绝不直接 `await connection.send()`；
  2. **大载荷加解密卸载线程池**：超过阈值（如 `> 64 KiB`）的 `pack`/`unpack` 经串行化序列号槽位在 `asyncio.to_thread` 中执行 AES-GCM + Base64 + JSON，保持事件循环亚毫秒响应；
  3. **合理化发送超时与背压**：将硬编码 `4s` 发送超时调整为按帧大小分级的动态超时（如 `10s + payload_mb * 2s`），并在超时前优先检查底层缓冲压力而非一刀切秒杀隧道。

---

## 3. 维度三：Token 浪费与可观测性盲区（浪费 Token 的地方）

### 3.1 `B4` [P0] 默认 `view=2` / `mirror="full"` 致 100% 双份响应，48KB 预算腰斩 + 双跑裁剪

- **源码铁证**：
  1. **默认开启 100% 双份镜像**（`hub/mcp.py:69-74`）：
     ```python
     default_view = os.getenv("CODEPIER_MCP_DEFAULT_VIEW", "2") if profile == "unified" else "2"
     view = query.get("view", default_view)
     verbosity = query.get("verbosity", "lean")
     lean_v3 = profile == "unified" and view == "3" and verbosity == "lean"
     text_mode = query.get("text", "compact" if lean_v3 else "json")
     mirror = query.get("mirror", "meta" if lean_v3 and text_mode == "compact" else "full")
     ```
     尽管代码库**已经完整实现了 `view=3` + `text=compact` + `mirror=meta`**（在 `hub/core_tools.py:442-473` 中仅在 `content[0].text` 放正文、`structuredContent` 用 `content_in: "text"` 指针替代双份正文，并在 `hub/core_tools.py:825` 与 `shared/mcp_presentation.py:1238-1247` 提供紧凑搜索结果、缩进树与精简 `open_workspace`），却因为默认环境变量 `CODEPIER_MCP_DEFAULT_VIEW="2"`，导致所有默认接入的智能体都落入 `mirror="full"`：
     - **每一条工具响应都在 `content[0].text`（完整 JSON 字符串）与 `structuredContent`（完全相同的 JSON 对象）中重复传输两遍**；
     - `bound_batch_response`（`hub/core_tools.py:546-644`）以整包 JSON-RPC `49,152` 字节为上限，双份镜像直接把有效内容上限从 ~46 KB 腰斩至 **~23 KB**（与实测 23,766 B 即触发 `budget_limited: true` 完全吻合），直接导致智能体**阅读大文件/批读的轮次翻倍、输入上下文 Token 翻倍**！
  2. **协作与错误回执硬编码双写**（`hub/mcp.py:504-516, 603-614`）：即便显式传了 `view=3&mirror=meta`，协作工具与所有 `isError: True` 回执仍硬编码同时写入完整 `content[0].text` 和 `structuredContent`。
  3. **`bound_batch_response` 连续跑两次**（`hub/core_tools.py:887-898` 与 `hub/mcp.py:806-817`）：先在 `core_tools.result()` 内跑一遍（含双 `deepcopy` + 二分 `json.dumps` 试算），回到 `hub/mcp.py:806` 后在事件循环主线程上**再完整跑第二遍**！

- **根治优化设计**：
  1. **将 unified profile 默认呈现升级为 `view=3`（`text=compact` + `mirror=meta`）**：通过 `CODEPIER_MCP_DEFAULT_VIEW="3"` 生效，同时保留 `?view=2&mirror=full` 给老旧强依赖 `structuredContent.content` 的客户端显式回退（并可通过 `clientInfo` 能力协商兼容）。
  2. **统一协作与错误回执的 `mirror=meta` 渲染**：错误回执在 `mirror=meta` 下仅在 `structuredContent` 保留精简 `{error_code, recovery_summary, next_call}`，不整包双写。
  3. **消除 `bound_batch_response` 重复执行**：在 `hub/mcp.py` 统一作为出站唯一出口执行一次裁剪（或给已裁剪 `result` 附内部已定界标记，第二处 $O(1)$ 跳过），避免事件循环上双倍 `deepcopy` 与二分 `json.dumps`。

---

### 3.2 契约设计缺陷：盲盒 `args` 契约税（高频动作试错往返浪费）

- **现状与实测证据**：
  `query` / `act` 为了压低 `tools/list` 首屏 Schema 体积，把数十种 action 的差异参数全部折叠进无结构的 `args: object`。但对于智能体最高频的 5 个动作（`file/batch`、`search`、`tree`、`exec`、`edit`），仅靠一句模糊 description 根本无法得知关键字段名（例如批量读是 `args.items=[{path, offset, limit}]` 而非 `start_line`/`end_line`；`edit` 写新文件需 `expected_sha256="new"` + `content`）。结果模型**要么猜错吃一次 `INVALID_ARGUMENTS`（+ 双份错误回执 Token），要么被迫先调一次 `query(action="help", ...)`（+1 次完整 LLM 推理轮次，通常消耗数千~上万上下文输入 Token）**。
- **第一性原理优化设计（高信息熵微速查表）**：
  无需恢复膨胀的嵌套 JSON Schema，只需在 `query` / `act` 的 `description` 或 `args.description` 中嵌入 **~350 字节的超紧凑动作速查表（Cheat Sheet）**：
  ```text
  Common args:
  - file: {path, offset?, limit?} or batch {items:[{path,offset?,limit?}]}
  - tree: {path?, depth?, offset?, limit?}
  - search: {query, path?, file_glob?, offset?, limit?}
  - edit: {path, expected_sha256:"new"|<sha>, content|edits:[{old_text,new_text}], idempotency_key}
  - exec: {command, cwd?, timeout_seconds?, idempotency_key}
  ```
  以一次性 ~90 Token 的静态成本，换取每次会话节省 2–5 轮试错/查 `help` 的数万 Token 往返开销（ROI > 50x）。

---

### 3.3 `B12`（计量盲区部分）[P2] `token_estimate` 系统性低估真实消耗达 2 倍

- **源码铁证**：`shared/token_estimate.py:28-65, 158-179, 251-294`：
  1. **2x 镜像盲区**（L251-294，核心在 L288-290）：`estimate_output(result)` 只要在 `content` 里找到了任意 `type="text"` 块（`found_text = True`），就直接忽略 `result["structuredContent"]`（`if not found_text and "structuredContent" in result:`）！而大多数现代 MCP 宿主（Claude Desktop、Cursor、Antigravity 等）会同时序列化或透传两者，或者至少按整包 JSON-RPC 字节承压 —— 导致 CodePier 自身统计面板看到的输出 Token 始终只有真实发送量的一半，**让 `B4` 的 200% 镜像浪费长期在监控体系中“隐身”**。
  2. **过度排除有效载荷**（L28-65, L158-179）：`EXCLUDED_KEYS` 把 `_meta`（L30）、`schema`/`inputSchema`/`outputSchema`（L61-63）以及非 `batch=True` 结构下的 `files` 键（L55, L158-179）全部剔除，导致 `tools/list` 巨型 Schema 开销与目录/变更列表中的 `files` 载荷被漏计。
- **根治优化设计**：
  拆分为清晰的双口径指标并入库展示：
  - `wire_tokens_est`（传输层真实载荷估算：涵盖 `content` + `structuredContent` + `_meta` 完整出站 JSON 字节，严守脱敏不存原文）；
  - `semantic_tokens_est`（去重后的业务有效载荷估算）；
  - 计算并暴露 **镜像膨胀比 `mirror_overhead_ratio = wire_tokens_est / max(1, semantic_tokens_est)`**，让任何未来的双写回退在监控与回归门禁中无所遁形。

---

## 4. 缺陷耦合关系与分层治理路线图（Wave 1 / Wave 2 / Wave 3）

### 4.1 关键依赖关系

1. **`B1` 与 `B14` 强耦合**：两者共同写入 `devices` 表触发 `security_epoch` 与 2x `session_revision`。**严禁只修 B1 不修 B14**，否则每 12s 心跳仍持续击穿只读并发。
2. **`B12（双口径计量）` 与 `B4（默认切 `view=3`/`mirror=meta`）前后衔接**：建议先合入 `B12` 计量修正（或在灰度中同时观测 `wire_bytes`），再切换 `B4` 默认值，以便在生产审计面板上拿到无可辩驳的 45%–50% 字节/Token 下降实证。
3. **`B3` 与 `B17` 共同释放 `StoreWorker`**：先解除 `B3` 同步路径的 7 项误占写线程，再清理 `B17` 后台每 5s 7 次扫表与单次读 2 次 `evidence` 写库，方可彻底消灭 `knowledge.maintenance.state = "deferred_busy"` 饥饿现象。

### 4.2 三阶段治理路线图

#### Wave 1：P0 止血 — 解锁并发 + 消除双倍 Token 税（最高 ROI）
- **目标**：消除轻并发阶梯串行，将默认有效载荷预算恢复至 ~46 KB，大幅降低 Hub/Agent fsync 压力。
- **核心改动**：
  1. **`B1` + `B14`**：拆出 `device_presence` 表（或列级触发器 + 显式 `security_write` 标志）；`_heartbeat()` 严格 Diff-before-Write 且 `scheduler` 运行态快照仅驻内存；`last_seen` 落盘节流至 60s。
  2. **`B3`**：Hub SQLite 开启 `PRAGMA synchronous=NORMAL`；`tools.resolve` 前置 `UNIFIED_TOOLS` 快速路径并缓存冻结 Schema；`origin_allowed`/`public_url`/`online`/`project`/`capture_output` 全部剥离 `StoreWorker`；单请求内按纪元复用 `principal`；`integrations.finish()` 合并写事务并异步低频清理 `mcp_activity`。
  3. **`B2`**：Agent `agent.sqlite3` 补齐 `synchronous=NORMAL` + `timeout=30`；剥离事件循环主线程同步 `journal.lock` 盘 I/O；修复 `telemetry.snapshot` 重复调用与 `"trace:" + id` 键泄漏。
  4. **`B12（双口径计量前置）` + `B4` + 盲盒 `args` 速查**：先上线 `wire_tokens_est` / `semantic_tokens_est` 与 `mirror_overhead_ratio` 观测基线（`B12` 计量部分），再将 unified profile 默认切为 `view=3`（`text=compact` + `mirror=meta`）；统一消除协作与错误回执无条件双写；消除 `bound_batch_response` 双跑；在 `query`/`act` 描述中嵌入 ~350B 高频 `args` 速查表。

#### Wave 2：P1 除障 — 根治智能体卡死、假死、死胡同与隧道雪崩
- **目标**：让所有分页、超时、取消、保活与隔离机制满足“控制面闭环自洽”，消除单点卡顿连坐全隧道的雪崩风险。
- **核心改动**：
  1. **`B5` + `B13`**：修复 `tree` `depth=1` 分页受 `per_dir=40` 锁死问题与字段语义混淆；修复 `search` 跳过二进制即误置 `truncated=True` 问题，引入 `scan_limited` 的 `resume_after_path` 真实文件游标。
  2. **`B6` + `B7`**：修复 `fail_fast` 在容量排队时穿透退化为无限等待；补齐 `max_queue_ms` 超时后的 `if not event.is_set()` 防误杀检查。
  3. **`B8` + `B9`**：允许只读主体取消自己发起的只读任务并补全 `cancellable_reads`；解耦 SSE keepalive/取消与 `plan is not None`，修复 `events()` 异常裸断流并提高单凭据流上限。
  4. **`B10`**：消除 `workspace_probe` 重复 `"root"` 阶段（4 次 RPC 降为 2 次）与重复 `integrations.project`；对齐 `ReadPool` 与 `read_limit`、`PROBE_TIMEOUT` 与 `wait`；将 `fs_search` 迁出 `execution` 通道；单项目无竞争时允许借满全部槽位。
  5. **`B16` + `B17`**：WS 读写队列解耦、大载荷加解密卸载线程池、动态化发送超时防止 4s 脆断连坐；协作后台合并为单次只读探测 + 空闲退避，`connection_evidence` 内存 60s 去重节流并消除 Fernet 二次解密。

#### Wave 3：P1/P2 深度演进 — LSP 常驻化、远端网关并发与计量闭环
- **目标**：将语义导航从冷启动秒级压至热查询百毫秒级，完善远端网关并发、异步收包与真实 Token 可观测性。
- **核心改动**：
  1. **`B15`**：落地 `LspPool` 常驻进程池 + 空闲 TTL + 增量文档同步，根除查完即杀。
  2. **`B11` + `B18`**：远端网关释放 `self.condition` 后再执行 `initialize()` 握手，支持配置化并发；Agent WS 收包循环中 `ack`/`probe` 彻底异步化。
  3. **`B12（并发部分）`**：`_wait()` 改为事件驱动单次等待消除 10s 切碎轮询与 `<1s` 余量丢失；缩小 `device_dispatch_lock` 临界区；压测验证后默认启用滚动投递。

### 4.3 预期量化收益对照

| 关键性能 / 效率指标 | 现状实测 / 源码基线 | Wave 1 完成后预期 | Wave 1–3 全量完成后预期 |
| --- | --- | --- | --- |
| 4 并发轻只读调用 P95 耗时 | `~9s`（明显阶梯 `2->6->7->9s`） | `< 1.5s`（真正并发完成） | `< 0.6s` |
| 单设备每 12s 心跳写库 & `session_revision` 抖动 | 2 次写事务 / 2 次 FULL fsync / **4 次** revision 自增 | **0 次**（仅真实变更或每 60s 写 `device_presence`） | **0 次**全局安全纪元污染 |
| 单次只读 `StoreWorker` 排队与 fsync 次数 | `~8–10` 次写队列排队、`3–5` 次 FULL fsync | `1` 次合事务 NORMAL 写（`0` 次冗余写） | 遥测异步批写，热路径接近 `0` 阻塞 |
| 单次响应有效负载上限（48 KiB 总预算下） | `~23 KB`（双份镜像腰斩 50%） | `~45 KB`（**有效吞吐 +95%**） | `~45 KB` + 真实双口径可观测 |
| `tree(depth=1)` / `search` 翻页死循环率 | `docs/` 86 文件不可达；含二进制即多扫 1 轮 | — | **0%**（游标严格闭环） |
| LSP 二次符号查询延迟 | `5–20s`（每次冷启冷索引查完即杀） | — | `< 100ms`（热池复用） |

---

## 5. 不可妥协的安全/一致性红线与自动化验证门禁

### 5.1 五条工程与安全红线

1. **鉴权即时失效零退化**：将心跳与运行态字段剥离出 `devices` 触发器后，任何真实的凭证吊销、Grant 过期、角色/Profile 变更、项目绑定修改仍**必须 100% 同步递增 `security_epoch` 与 `session_revision`**，绝不允许出现吊销后的陈旧权限缓存穿透。
2. **已提交写回执的持久原子性**：Hub 与 Agent 切换为 `WAL + synchronous=NORMAL` 后，进程崩溃恢复语义必须与现有 `tests/test_recovery_integration.py`、`tests/test_hub_recovery_and_event_delivery.py` 完全一致；绝不允许为了提速把写操作的幂等账本（`calls`、`operations`）降级为纯内存态。
3. **只读取消权限最小化**：放宽 `B8` 允许只读主体取消慢读时，必须严格校验 `op["tool"] in READ_ONLY_TOOLS` 且属于当前 `principal` 本人/同一 `grant_id`，严禁跨用户越权取消或误取消写操作。
4. **协议向后兼容与诚实降级**：切换默认 `view=3` / `mirror=meta` 时，完整保留 `?view=2` 与 `?mirror=full` 查询参数；SSE 流式通道在任务失败时必须输出规范 JSON-RPC 错误帧后再关闭，绝不静默截断。
5. **脱敏边界与零明文留存**：修正 `B12` 真实 Token 计量口径时，仅统计出站字节与估算值，**严禁记录或持久化工具参数与响应明文**，继续严守既有脱敏契约。

### 5.2 现有必跑回归测试集

落地每个 Wave 后，必须全量通过以下受影响模块的既有自动化测试：

```sh
python -m pytest -q \
  tests/test_bounded_batch_read.py \
  tests/test_mcp_token_presentation.py \
  tests/test_output_fidelity_ranges.py \
  tests/test_mcp_catalog_modes.py \
  tests/test_mcp_output_fidelity.py \
  tests/test_mcp_gateway.py \
  tests/test_mcp_gateway_protocol.py \
  tests/test_resource_semantics_v2.py \
  tests/test_queue_hub_optimizations.py \
  tests/test_hub_adaptive_admission.py \
  tests/test_hub_recovery_and_event_delivery.py \
  tests/test_recovery_integration.py \
  tests/test_connection_evidence.py \
  tests/test_adaptive_agent_integration.py
```

### 5.3 建议新增的专项防回归断言（逐项锁定 B1–B18）

| 建议新增测试用例名 | 锁定缺陷 | 核心断言 |
| --- | :---: | --- |
| `test_heartbeat_without_security_change_keeps_security_epoch_and_revision_stable` | **B1, B14** | 连续模拟 10 次带相同元数据的 Agent 心跳，断言 `security_epoch`、`session_revision` 均增长 `0` 次，且期间并发 `run_read()` 零重试、零写锁栅栏；随后撤销设备凭证，断言两者立即递增并拦截后续请求。 |
| `test_agent_journal_uses_wal_normal_and_never_blocks_event_loop` | **B2** | 断言 `agent.sqlite3` 启动后 `PRAGMA synchronous == 1 (NORMAL)`；模拟慢盘 `journal.finish()` 期间，事件循环心跳/状态响应延迟 `< 20ms`；任务完成后 `pending_output` 不残留 `"trace:" + id`。 |
| `test_unified_tool_resolve_skips_schema_deepcopy_and_capture_output_off_writer` | **B3** | 对 `query`/`act` 调用打桩断言 `tools.resolve()` 不触发 `definitions()` 深拷贝；断言 `capture_output()` 不在 `hub-sqlite` 写线程运行；单次调用 `integrations.finish()` SQLite 提交次数 `<= 1`。 |
| `test_default_unified_view_uses_meta_mirror_and_single_bound_pass` | **B4** | 默认参数下批量读 40 KB 文本不触发 `budget_limited`，且 `structuredContent` 仅含指针（总 JSON 字节 `< 49152`）；打桩断言单次 `/mcp` 仅调用 `bound_batch_response` 1 次。 |
| `test_tree_depth1_paginates_past_per_dir_limit` | **B5** | 在包含 120 个文件的目录执行 `tree(depth=1, limit=40)`，循环跟随 `next_offset` 断言能不重不漏遍历全部 120 项，不会出现 `truncated=True` 且 `next_offset=None`。 |
| `test_search_skipped_binary_never_sets_truncated_or_phantom_next_offset` | **B13** | 仓库含二进制大文件与 3 条文本匹配，执行 `search(limit=50)` 断言 `skipped_reasons` 非空但 `truncated is False` 且 `next_offset is None`；触发 `scan_limited` 时断言返回有效 `resume_after_path` 且下一页可继续推进。 |
| `test_fail_fast_rejects_capacity_wait_and_max_queue_ms_preserves_admitted` | **B6, B7** | `lane_capacity` 满载无资源锁冲突时，`wait_policy="fail_fast"` 立即抛 `RESOURCE_BUSY` 不挂起；`max_queue_ms` 超时与 `event.set()` 同 tick 竞态时，已进入 `self.active` 的任务不被误杀。 |
| `test_readonly_principal_can_cancel_own_slow_reads_including_knowledge` | **B8** | 仅具 `read` scope 的凭证可取消自己发起的 `fs_search` / `open_workspace` / `project_knowledge_query`，且不可取消他人或 `exec` 操作。 |
| `test_sse_keepalive_works_for_non_unified_profiles_and_emits_error_frame` | **B9** | `core`/`coding` profile 长等待可收到 `: keepalive` 且响应 `notifications/cancelled`；流启动后内部抛异常时客户端能收到完整 JSON-RPC error SSE 帧而非连接被裸截断。 |
| `test_read_probe_single_root_phase_and_fs_search_not_starved_by_exec` | **B10** | 单次 `read` 仅产生 1 次 `"root"` 阶段 RPC；`execution` 通道被长 `exec` 占满时，`fs_search` 仍能在 `< 1s` 内调度执行；`ReadPool.limit == scheduler.config["read_limit"]`。 |
| `test_lsp_pool_reuses_warm_server_across_queries_and_evicts_on_idle` | **B15** | 连续两次 `symbols` 查询复用同一 LSP 进程 PID，第二次耗时显著低于冷启动，超时空闲后进程被干净回收。 |
| `test_ws_large_payload_does_not_kill_sibling_inflight_operations` | **B16, B18** | 同连接并发发送大载荷与多个轻读及 `ack` 时，事件循环不卡死、不触发 4s 误杀整条 WS 隧道、所有并发任务正常收敛。 |
| `test_collaboration_idle_poll_and_connection_evidence_throttled` | **B17** | 空闲 15s 内协作后台写库次数 `== 0`；同 grant 10s 内连续 20 次读仅写 `mcp_connection_evidence` 1 次，且不触发 Fernet 解密回查。 |
| `test_remote_mcp_initialize_releases_condition_and_supports_concurrency` | **B11** | 模拟远端 MCP `initialize()` 慢握手 2s，断言 `close()` 与其他并发等待者不会被 `self.condition` 锁死；配置显式并发上限后多请求可并行派发。 |
| `test_unified_wait_single_event_and_dispatch_lock_scoped` | **B12（并发）** | `wait=25` 下仅触发 1 次事件等待而非 3 轮 10s 切碎 `process` 轮询，且 `0.5s` 剩余时间窗不被丢弃；新操作入队不与投递准备互相阻塞。 |
| `test_token_estimate_reports_wire_and_semantic_tokens_for_full_mirror` | **B12（计量）** | `mirror="full"` 响应下，`wire_tokens_est` 约等于 `2 * semantic_tokens_est` 且 `mirror_overhead_ratio >= 1.8`，监控不再漏计双份镜像。 |
