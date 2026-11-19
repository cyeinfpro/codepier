# CodePier (`MCP`) 未提交修改深度审查报告（2026-11-20）

> **审查对象**：`main` 分支 HEAD `d9dfa13` 之上的全部未提交修改（49 项：43 个已修改文件 + 6 个未跟踪文件，`+4877 / −998` 行）。
> 这批修改对应 `docs/MCP_AGENT_FIRST_PRINCIPLES_DIAGNOSIS_2026-11-19.md` 中 B1–B18 的落地实现。
>
> **审查方法**：逐文件通读 `git diff`，并对每处行为变化反查调用方与数据读者（grep 全仓）；对高危结论写了最小复现脚本，在本机 `.venv`（Python 3.13.12）里实际运行；最后跑了变更相关测试与诊断文档 §5.2 规定的回归集。
>
> **结论先行**：性能方向的大部分改动是对的（心跳 diff-before-write、列级触发器、调度器 B6/B7、SecureChannel 串行锁、tree/search 游标等）。但有 **1 个 P0**：会在 Agent 崩溃后**重复执行带副作用的写操作**，直接违背诊断文档 §5.1 红线 #2。另有 **4 个 P1**：功能失效、资源锁可被绕过、已启动的读被误杀、吊销流程与其它步骤事务耦合。现有测试（763 个通过）**没有一个能覆盖到这些问题**。**建议修完 P0/P1 再合入。**

---

## 0. 测试与验证结果

| 测试集 | 结果 | 说明 |
| --- | --- | --- |
| 变更相关 15 个测试文件（含 5 个新增 `test_b*`） | **389 passed / 3 failed** | 3 个失败均为 `tests/test_mcp_catalog_modes.py` 的目录体积预算用例，**在 HEAD 上同样失败**（HEAD：31150 / 97707 / 33765 字节；工作区：31197 / 97759 / 33765 字节）。属于既有问题，本次改动又让体积多了约 50 字节（`_CHEAT_SHEET` 速查表） |
| 诊断文档 §5.2 必跑回归集（除已单列的 catalog 外的 12 个文件） | **374 passed** | — |
| 最小复现脚本（附录 A） | P0-1、P1-2、P1-3 均复现 | 输出见各条目 |

**测试环境注意**：通过 CodePier `exec` 跑 pytest 时，`TMPDIR` 位于 `~/.codepier-agent/state/project-runtimes/.../tmp`，被 `FileEngine` 判定为受保护目录。结果是 `test_agent_journal_uses_wal_normal_and_never_blocks_event_loop` 报 `PROTECTED_ROOT` 失败，紧接着 `test_trace_key_cleaned_on_cancel_before_start` **永久挂死**（见 P3-1）。设置 `TMPDIR=/tmp/...` 后这两个用例均通过。上表结果都是在设置 `TMPDIR` 之后得到的。

---

## 1. 问题总览

| 编号 | 级别 | 一句话 | 位置 |
| --- | :---: | --- | --- |
| **P0-1** | **P0** | `mark_running` 改为“先内存、后异步落盘”。崩溃后，已开始的写操作会被判为 `retryable`，被 Hub 重新投递并**重复执行** | `agent/runner.py:228-275, 1560`；`agent/journal.py:72, 231` |
| **P1-1** | P1 | `devices.last_seen` 不再有人写，但仍有两个读者：面板滚动升级**永远无法判成功**，任务存活度恒为 stale | `hub/panel_agent_rollout.py:371,387`；`hub/collaboration/task_liveness.py:202` |
| **P1-2** | P1 | 调用方自报 `read_only=True` 即可把显式 `write` 资源锁、本地独占 task 策略**降级为共享**，绕过其它操作的写锁 / `read_stable` | `agent/resource_queue.py:510-552`；`agent/runner.py:1514` |
| **P1-3** | P1 | 读操作**已经开始执行**后，探针超时仍按“首次执行期限”截断，可降到 0 秒 → 刚 `started` 就超时失败 | `agent/runner.py:1037-1060, 1540, 1927` |
| **P1-4** | P1 | 协作后台把吊销清理、证明过期、reconcile、事件恢复、派发合进**同一事务**：任一步失败会回滚其余步骤，持续失败时吊销任务永远提交不了 | `hub/collaboration/lifecycle.py:242`；`hub/collaboration/events.py:1897` |
| P2-1 | P2 | `wake_reconcile` / `wake_renewals` 没有任何调用方，新的吊销任务、续订要等 20 秒空闲退避（原为 5 秒） | `hub/collaboration/lifecycle.py:112-121` |
| P2-2 | P2 | 后台“先探测后干活”的 SQL 与真正的恢复逻辑是两份实现，且有几个条件实际恒为真：空闲退避经常失效，后续还会漂移出漏恢复 | `events.py:_probe_tick`、`lifecycle.py:_probe_reconcile` |
| P2-3 | P2 | `Runtime.online()` 移出 StoreWorker 后，缓存未命中时会在**事件循环线程**同步查 SQLite 并等写锁 | `hub/runtime.py:620-629, 2188` |
| P2-4 | P2 | `Journal.terminal_statuses` 只增不减；历史 id 被 retention 清理后，状态从 `missing` 变成 `done` | `agent/journal.py:131-143, 326` |
| P2-5 | P2 | `_classify_write` 对未知表 fail-open（与 docstring 的 “failing closed” 矛盾）；安全表白名单是硬编码的 | `hub/store.py:227-290` |
| P2-6 | P2 | Hub 与 Agent 默认改为 `synchronous=NORMAL`：断电会丢最近的提交（含吊销、`mark_running`），会放大 P0-1 | `hub/store.py:438`；`agent/journal.py:33` |
| P2-7 | P2 | `delivery_rolling_devices` 默认值从“仅金丝雀”改为 `"*"`（全量），属于发布行为变更 | `shared/config.py:84, 99` |
| P2-8 | P2 | Hub 出站帧改为“控制帧优先”，打破了原先同连接严格 FIFO；控制队列满即断开连接 | `hub/runtime.py:_CONTROL_FRAME_TYPES / send_nowait` |
| P3-x | P3 | 测试会无限自旋；`_wait` 多一次往返；网关显式并发会覆盖旧协议串行；LSP 同项目串行等 | 见 §4 |

---

## 2. P0 / P1 详述

### P0-1 `mark_running` 异步化破坏“已开始”预写日志，崩溃后写操作被重放

**改动**：所有 `self.journal.mark_running(id)`（同步写 SQLite）都换成了 `self._mark_running(id)`（`runner.py:272`）。新函数只改内存里的 `active_states`，然后把 `mark_running_db` 放进 `_journal_persist_queue`，由后台 task 通过 `disk_call` 批量写盘。之后代码**立刻** `send({"type":"started"})` 并进入 `execute()`，也就是开始产生副作用。

**为什么是 P0**：整条恢复链依赖一个不变量：“DB 里是 `accepted`，就说明还没开始”。

1. `agent/journal.py:72`：Agent 启动时执行 `UPDATE calls SET status='retryable' WHERE status='accepted'`，注释写着“accepted means … never started”；
2. `hub/runtime.py:3718-3746`：Hub 收到 `retryable` 后，按注释“retryable is issued only for a read or a command that had not started … Never rerun side effects”，把操作重新置为 `queued` 并再次投递；
3. `journal.start()` 对 `retryable` 记录返回 `None`，也就是**再执行一次**。

于是，从 `_mark_running` 到后台落盘完成之间，`exec` 子进程已经启动、写文件已经开始。只要这段窗口里 Agent 崩溃（OOM、`kill -9`、升级重启、断电），这个写操作就会被**完整重放一遍**。`disk_call` 线程池忙的时候（`finish`、ack 批量、telemetry flush 都在排队），这个窗口会明显变长。

**复现**（附录 A，R1）：

```text
start -> None
in-memory status: running
after restart DB status: retryable
re-start same id -> None (None == will execute AGAIN)
old sync path after restart: interrupted      ← 修改前的语义：中断、不重跑
```

这直接违反诊断文档 §5.1 红线 #2：“绝不允许为了提速把写操作的幂等账本（`calls`、`operations`）降级为纯内存态”。

**修复建议**：
- 状态迁移 `accepted → running` 必须在任何副作用**之前**持久化。可以写 `await self.disk_call(self.journal.mark_running_db, id)`：它不阻塞事件循环（B2 的初衷），但会等落盘完成；
- 只对 `scope == "read"` 的工具允许内存态 / 批量落盘（读重放本来就是允许的）；
- 新增测试：`_mark_running` 之后、persist task 完成之前重建 `Journal`，断言非读工具的状态是 `interrupted` 而不是 `retryable`。
- `cancel_memory + 异步 cancel_db` 同样有崩溃丢失窗口。不过 Hub 侧有 `op["cancel_requested"]` 兜底（`runtime.py:3732`），风险较低，可在同一处一起改为等落盘完成。

---

### P1-1 `devices.last_seen` 已冻结，但仍有读者 → 滚动升级必判失败、存活度恒 stale

**改动**：`_touch_agent` / `_register_agent` 不再 `UPDATE devices SET last_seen`，改写 `device_presence`（每 60 秒一次）。展示路径（`api/devices.py`、`diagnostics.py`、`connection_evidence.py`）已经改用 `runtime.device_last_seen()`，但 grep 后还有两处**直接读 `devices.last_seen`**：

1. `hub/panel_agent_rollout.py:371/387`：`device = SELECT * FROM devices`，用 `device["last_seen"] > op["created"]` 判断“升级后已重连”。现在这个字段停在迁移那一刻，条件永远为假，900 秒后走到 `“更新已交接，15 分钟内未核对目标版本”`，节点**一律判为失败**。面板的 Agent 批量升级功能实际上不可用了。
2. `hub/collaboration/task_liveness.py:202`：`MAX(d.last_seen) AS last_node_contact_at`，再用 `freshness(..., 45)` 判断新鲜度，结果恒为 stale。还要注意：即使改读 `device_presence`，它的落盘周期是 60 秒，**比 45 秒的新鲜度窗口还长**，同样会抖动。

全仓已经没有任何 `SET ... last_seen` 写 `devices` 表了（已 grep 确认）。

**修复建议**：rollout 改用 `self.runtime.device_last_seen(device_id, device["last_seen"])`，或者直接用连接建立时间；task_liveness 改为 `LEFT JOIN device_presence`，并在 Python 侧用在线连接的内存 `last_seen` 覆盖，新鲜度阈值也要与 `PRESENCE_PERSIST_INTERVAL` 对齐。另外建议加一个测试：grep 断言 `devices.last_seen` 不再被任何 SQL 读取。

---

### P1-2 调用方自报 `read_only` 可绕过资源互斥

**改动**（`agent/resource_queue.py:510-552`、`runner.py:1514`、`shared/scheduler_config.py:is_read_only_exec`）：`exec` 只要带上 `read_only: true`（或 `options.read_only`），就会：
- 让 `slot_write=False`；
- 把显式声明的 `resources[*].mode == "write"` / `"read_stable"` 路径锁**降级为普通读锁**；
- 把本地配置为独占的 `task`（`allow_read_concurrency` 未开）也降级为共享。而同一函数里原有的注释正是 *“This is local task policy, never caller-declared resource policy.”*

`read_only` 只是一个自报标志，命令本身照样可以写文件。降级之后，**其它智能体或操作持有的写锁、`read_stable` 稳定性承诺都会被它穿透**。多智能体协作场景下，这是一个一致性漏洞。

**复现**（附录 A，R3）：

```text
plain exec                    -> write=True
read_only + resources write   -> write=False            ← 显式写锁被降级
task deploy (exclusive)       -> write=True,  stable=True
task deploy + read_only       -> write=False, stable=False   ← 本地独占策略被覆盖
```

**修复建议**：`read_only` 最多只能用来**省掉隐式的 cwd 写锁**（未声明 `resources` 时）。它不应覆盖显式 `write`/`read_stable` 声明，也不应覆盖本地 task 策略。理想做法是由本地配置（task 白名单或命令策略）决定，而不是相信调用方。

---

### P1-3 已开始的读操作，执行阶段超时仍被“首次执行期限”截断（可为 0 秒）

**改动**：`_read_execution_worker(..., deadline=not_after)` 把 `not_after` 存进 `_read_worker_deadlines`，`execute()`（`runner.py:1927`）再用它算探针超时：`min(30, not_after − now)`，不加裕量。`not_after` 就是 Hub 的 `op["deadline"] = created + queue_seconds`（`hub/runtime.py:2494-2507`），每个操作都有。它的语义是**“最晚开始时间”**，不是执行时限。

后果：在队列里排得越久，读操作真正执行时拿到的预算越少。如果在期限前一刻开始，预算就是 `0.0`。`effective_probe_timeout(0.0) == 0.0`，`asyncio.timeout(0.0)` 会立即抛 `TimeoutError`，结果是**刚发出 `started` 就失败**。拥塞时恰好最容易触发，与 B10 的目标相反。

**复现**（附录 A，R2）：

```text
effective_probe_timeout(0.0) = 0.0
Agent._probe_timeout(deadline in past, no margin) = 0.0
asyncio.timeout(0.0) -> TimeoutError immediately
```

**修复建议**：`not_after` 只用于 admission / root 这类**开始前**的阶段；`execute()` 阶段继续用 `PROBE_TIMEOUT`（或调用方 `wait`），并设下限（例如 `max(5, …)`）。

---

### P1-4 协作后台多步工作合并为单事务：失败互相回滚，吊销可能永远不提交

**改动**：
- `lifecycle.py:242 _run_reconcile`：`drain_revoke_jobs()`、`host_proofs.expire()`、`service.reconcile()`、`resume_host_backfill()` 放在**同一个** `store.transaction()` 里；
- `events.py:1897 _run_tick_work`：`recover_paused_work`、`dot_relay.recover`、`mailbox.recover`、`room_consumers.recover`、`task_liveness.recover` 和**派发 `reserve()`** 放在同一事务里。

修改前每一步都是独立的 `store.run`，各自提交。现在只要其中一步抛异常（例如某条坏数据让 `reconcile()` 持续失败），已经完成的**宿主吊销清理、证明过期**也会一起回滚，并且每个 tick 都重演一遍。结果是吊销永远完成不了，这触及红线 #1“鉴权即时失效”。events 侧同理：任一恢复步骤失败，本 tick 的 `reserve()` 就不会执行，持续失败时事件投递整体停摆；反过来，`reserve()` 失败也会把已完成的恢复唤醒一并回滚。另外，长事务会占住唯一写线程，与 B3 的目标相悖。

**修复建议**：恢复原来“每步独立提交”的结构。如果确实想合并，就给每步套 `savepoint()` 并单独 `try/except` 计数，至少保证 `drain_revoke_jobs` / `expire` 独立提交；`reserve()` 必须与恢复步骤解耦。

---

## 3. P2 详述（简要）

**P2-1 唤醒函数无人调用**：新增的 `wake_reconcile()`、`wake_renewals()` 在全仓**没有调用方**（只有 `wake_events`、`wake_monitor` 被接上了）。reconcile / 续订循环空闲时按 `IDLE_BACKOFF_SECONDS = 20` 休眠。`room_host_connections.py:483` 插入新的 `room_host_revoke_jobs` 之后，最长要 20 秒以上才会被清理（原来是 5 秒）。建议在插入吊销任务、批准续订等写路径加 `store.after_commit(loops.wake_reconcile)`。

**P2-2 探测逻辑与恢复逻辑是两份实现**：`_probe_tick` / `_probe_reconcile` 用约 20 条手写 SQL 去“猜”各个 `recover()` 有没有活要干。问题有两个：
- 一些条件实际**恒为真**：`coordination_goals WHERE state='active'`、`room_waiting_inputs JOIN 活跃房间`、`task_liveness` 的“300 秒无进展”探测没有检查已唤醒的 episode。只要存在任意一个长期活跃的目标或卡住的任务，空闲退避就永远不生效，B17 的收益在真实负载下打折扣；
- 以后有人修改 `recover()` 却忘了改探测，就会**静默漏恢复**。

建议让每个 `recover()` 返回“是否做了事”，或者提供与它放在同一处的 `has_work()`，由结果驱动退避。

**P2-3 `online()` 在事件循环上同步查库**：`submit` 路径（`runtime.py:2188`）把 `await self.store.run(self.online, …)` 改成了直接调用。`online()` 缓存未命中时（`session_revision` 一有安全写就变）会走 `connection_authorized → store.one`，在**事件循环线程**里获取 `store.lock`，与写线程竞争（写事务期间要等提交）。建议 `online()` 只看内存连接状态，鉴权部分放到 `run_read` 里；或者改用 `security_epoch` 做缓存键。

**P2-4 `terminal_statuses` 无界增长**：`Journal` 启动时把全部 `calls` 的终态载入内存，之后每完成一次就加一条，`journal_retention.prune` 不清理它。长时间运行的 Agent 内存会单调增长。另外，已被 retention 删除的 id，`fast_status` 仍返回 `done`（以前是 `missing`），`cancel_pending_call` 对这类 id 的判断随之改变。建议在 prune 之后同步清理，或者改为 LRU 上限。

**P2-5 写分类器对未知表 fail-open**：`_classify_write` 的 docstring 写着 “failing closed on unknown/complex SQL”，但在 `UPDATE/INSERT/DELETE` 已解析出表名、且表不在 `_SECURITY_TABLES` 时，返回的是“非安全写”。旧正则 `gateway_[a-z_]+` 会自动覆盖新增的 gateway 表，现在改成了硬编码列表。`conversation_project_revocations`、`room_permission_policies`、`room_member_scopes`、`oauth_clients` 这类名字明显带授权语义的表都不在列（旧正则也没覆盖，所以**不是回归**）。但现在 `session_revision` 被用作多个缓存的失效键（`connection_authorized(allow_cache)`、`_revalidate_principal`、证据上下文），覆盖面的重要性比以前高了。建议加一个测试：枚举 `sqlite_master` 里的全部表，要求每张表都显式归类为 security / snapshot / other，新表未归类时 CI 失败。

**P2-6 `synchronous=NORMAL` 的持久性代价**：WAL+NORMAL 下进程崩溃不会丢数据，但**断电或内核崩溃会丢最近的提交**。Hub 侧可能丢的是凭证吊销、角色变更（与红线 #1 冲突）；Agent 侧可能丢的是 `mark_running` / `finish`，与 P0-1 叠加后，即使 P0-1 修好了，断电仍可能导致重放。建议 Agent 的 `calls` 状态迁移保持 `FULL`（或单独 `fsync`）；Hub 至少在发布说明中写清楚，并提供 `CODEPIER_SQLITE_SYNCHRONOUS=FULL` 的推荐场景。

**P2-7 滚动投递默认全量开启**：`shared/config.py` 原注释为 “Only enrolled canary devices use this chunk size and refill”，现在默认 `("*",)`，`HUB_DELIVERY_ROLLING_DEVICES` 的默认值也是 `"*"`。诊断文档 Wave 3 自己写的是“压测验证后默认启用”。本次没有附压测证据，建议单独发布或保持默认关闭。

**P2-8 出站帧重排**：`Connection` 改为控制队列 + 数据队列，`ack/cancel/heartbeat/going_away/scheduler_config` 会越过已排队的数据帧。目前 Agent 能正确处理“`cancel` 先于 `call` 到达”（依赖 `journal.cancelled_ids`），但这是隐式依赖，没有测试锁定。另外，接收循环 `send_nowait(reply)` 失败就 `break` 断开连接：一个大载荷正在发送（超时可达 `10s + 2s/MB`）时，突发超过 512 个 ack 就会把控制队列塞满并触发断连。建议补“`cancel` 先于 `call`”和“控制队列背压”两个测试，满了的时候改为 await 而不是断开。

---

## 4. P3 / 观察项

1. **测试会无限自旋**：`tests/test_b2_b6_b7_b10_b18_agent_core.py:134` 的 `while not instance.scheduler.waiting: await asyncio.sleep(0)` 没有超时。被测路径一旦提前失败（如上文的 `PROTECTED_ROOT`），整个 pytest 进程永久挂死，本次实测挂了 6 分钟以上。建议用 `asyncio.timeout(2)` 包住，并在 conftest 里固定 `tmp_path` 根目录。
2. **`_wait` 判断恒假**：`hub/unified_tools.py:326` 的 `isinstance(row, dict) and "operations" in row`，`wait_for_operation` 返回的是原始 `operations` 行，永远没有 `"operations"` 键。所以每次都会多一次 `invoke("process", wait_seconds=0)`。功能正确（这次调用本身也是一次必要的重新鉴权），但与注释意图不一致，可以把判断删掉、直接走重新鉴权读取。
3. **远端网关显式并发覆盖协议约束**：`hub/gateway/remote.py:_effective_concurrency`。只要 connector 显式配置了 `concurrency`，legacy 或 stateful 会话也会并发派发，而修改前这类会话强制串行。需要确认 legacy SSE 传输实现是否按 JSON-RPC id 正确关联响应，否则应只对 MODERN 加 stateless 生效。
4. **LSP 常驻池按 `(root, language)` 加单锁**：同一项目的语义查询现在是串行的（以前每次查询起独立进程，可以并行）。冷索引期间后续查询会排队。可以接受，但建议在 `stage` 里暴露 `waiting_lsp`。
5. **`cancel()` 去掉了 `device_dispatch_lock`**：现在与投递准备（改用 `device_delivery_lock`）并发执行，安全性依赖 `before_send` 的二次校验。建议补“取消与投递同 tick 竞态”的测试。
6. **search 游标的两种序**：`git_index` 作用域按码点字符串排序，walk 作用域按 `(文件优先, casefold, name)` 排序。同一 path 在两次调用之间切换作用域时（例如 git 索引不可用），`resume_after_path` 会漏项或重复。建议在返回里带上 `scan_scope`，续扫时校验一致。
7. **Telemetry 延迟落盘静默丢弃**：`_pending_db` 超过 8192 条直接丢；`flush` 失败时整批丢弃（批次在写之前就已清空）。只影响诊断数据，建议计数并暴露出来。
8. **`_FrozenDict` 拦不住 `|=`**：CPython 的 `dict.__ior__` 不会调用被覆写的 `update`，`d |= {...}` 会静默修改冻结的 schema。目前全仓没有这种写法，属于预防性提示。
9. **unified profile 下 legacy 工具强制 view=2**（`hub/mcp.py:604-613`）：这里只覆盖了 `view/text_mode/mirror` 三个局部变量，`presentation_options` 里按 view=3 推导出的 `lean_v3` 等派生值没有一并回退。建议在这里重新调用一次 `presentation_options`。

---

## 5. 做得好的地方（保留）

- **B1/B14**：心跳各字段 diff-before-write；调度快照只留在内存；`devices` 的 UPDATE 触发器改为 `AFTER UPDATE OF id,secret,enabled,space_id,owner_user_id`；迁移时比对触发器 SQL 后重建。`_read_phase` 快路径增加 `schema_before == schema` 校验，比原来更严。
- **B6/B7**：`fail_fast` 在容量排队时立刻失败；`max_queue_ms` 超时前先检查 `event.is_set()`；`_abort_waiting` 在锁内清理干净。
- **B16**：`SecureChannel.pack_async/unpack_async` 在锁内完成“分配序号 → 加密 → 推进序号”，被取消时不消耗序号；Hub 单写协程保证了序号顺序。
- **B5/B13**：`tree(depth=1)` 不再受 `per_dir` 限制；`search` 区分 `page_full` 与 `scan_limited`，跳过二进制文件不再误报 `truncated`。
- **B8**：只读取消限定为“同 space、同 grant/用户、读 scope 的远端只读工具”，`room_scope` 与 `_cancel` 两处一致。

---

## 6. 建议修复顺序

1. **P0-1**（必须先修）：非读工具的 `accepted → running` 同步落盘，并补崩溃重放测试。
2. **P1-2、P1-3**：都是小改动，各加一个单测。
3. **P1-1**：两处读者改用 presence / 内存值，并加“禁止读 `devices.last_seen`”的守卫测试。
4. **P1-4、P2-1**：拆回独立事务；接上 `wake_reconcile` / `wake_renewals`。
5. 其余 P2 按发布节奏处理；P2-6、P2-7 至少写进发布说明。

---

## 附录 A：最小复现脚本

放在仓库外运行：`.venv/bin/python -I repro_core.py "$(pwd)"`。

```python
import asyncio, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])

# R1: mark_running 仅内存 → 模拟崩溃重启
from agent.journal import Journal
d = Path(tempfile.mkdtemp()); j = Journal(d)
req = {"tool": "exec", "project": "p", "args": {"command": "echo side-effect >> log"}}
j.start("op1", req); j.mark_running_memory("op1"); j.db.close()
j2 = Journal(d)
print(j2.status("op1")["status"], j2.start("op1", req))   # retryable None  → 将再次执行

# R2: 已开始读的探针预算可为 0
from agent import workspace_probe
from agent.runner import Agent
print(workspace_probe.effective_probe_timeout(0.0), Agent._probe_timeout(time.time() - 1))

# R3: read_only 降级显式写锁 / 本地独占 task
from agent.resource_queue import claims_for
class E: config = {"tasks": {"deploy": {}}}
root = Path(tempfile.mkdtemp())
print(claims_for(E(), "exec", {}, {"command": "make", "cwd": ".", "read_only": True,
      "resources": [{"kind": "path", "name": "build", "mode": "write"}]}, root))
print(claims_for(E(), "exec", {}, {"command": "make", "cwd": ".", "task": "deploy",
      "read_only": True, "resources": []}, root))
```
