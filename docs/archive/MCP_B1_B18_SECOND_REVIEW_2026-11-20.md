# CodePier (`MCP`) B1–B18 与二轮修复全量深度代码审查报告（2026-11-20）

> **审查范围**：`main` 分支基线 `d9dfa13b885aa3a3709be26a1718a23d5df154e9` 之上的全部 61 项未提交修改（50 个已修改文件 `+5,107 / -1,022` 行 + 11 个新增测试与文档文件），并完整覆盖审查期间外部并发合入的三笔提交（`45e6b49c` → `5535de70` → `edd5540c09e7cd04ef2c9d27931ee5abca8cb5b8`）。
>
> **行号基准**：本文全部源码定位均严格对齐最新 `HEAD`（`edd5540c`）。
>
> **总体结论**：
> - **P0 清零**：首轮审查指出的写操作异步 `mark_running` 崩溃重放（P0-1）、调用方自报 `read_only` 穿透显式锁与本地独占策略（P1-2）以及 `SQLite synchronous=FULL` 掉电持久性（P2-6）均已在代码层闭环修复。
> - **门禁与回归现状**：经 `edd5540c` 拆分超标函数后，`ruff`、`mypy`、宿主 Python 3.9 检查、仓库契约检查与函数复杂度棘轮（623 历史基线）均已通过；20 个变更测试文件（`460 passed`）与 §5.2 必跑 14 个核心回归文件（`412 passed`）全部通过。
> - **发版阻塞项**：在最新 `edd5540c` 代码上仍存留 **10 项 P1 阻断级缺陷**、**15 项 P2 中风险隐患**、**11 项 P3 改进项**、**4 处假桩短路单测**，以及 **3 份误写入 `shared/source_inventory.json` 的临时过程文档**。建议完成 P1 定向修复与过程文档清理后再推送正式发布标签。

---

## 0. 静态检查、门禁与回归测试实测矩阵

审查启动时（`01:46 +08:00`）工作区处于 `d9dfa13b` + 61 项未提交态；`01:48` 至 `02:09` 期间外部并发会话依次提交了 `45e6b49c`（v1.33.31 主提交）、`5535de70`（修复 `token_estimate.py` 类型收窄）与 `edd5540c`（重构辅助函数以满足复杂度棘轮）。以下全部门禁均在隔离临时目录（`TMPDIR=/tmp/...`）中实测：

| 门禁类别 | 实测执行命令 | 61 项未提交态退出码 | `edd5540c` 退出码 | 关键实测证据与说明 |
| --- | --- | :---: | :---: | --- |
| Ruff 语法与规则检查 | `.venv/bin/python -m ruff check agent hub shared scripts tests` | `0` | `0` | `All checks passed!`（含 `ruff.toml` 中 `py39` 目标脚本规则） |
| Ruff 格式检查（CI 口径） | `.venv/bin/python -m ruff format --no-cache --check agent hub shared scripts` | **`1`** | `0` | 未提交态有 18 个生产文件未格式化；当前 `453 files already formatted`（注：CI 未纳管的 `tests/` 下仍有 709 个文件未按 ruff 格式化） |
| Mypy 静态类型门禁 | `.venv/bin/python -m mypy` | **`1`** | `0` | `5535de70` 修复了 `shared/token_estimate.py` 的 `Projection \| None` 联合属性报错，15 个纳管核心文件通过 |
| 函数复杂度棘轮门禁 | `.venv/bin/python scripts/check_function_complexity.py` | **`1`**（63 项） | `0` | `45e6b49c` 曾扩大至 67 项违规；`edd5540c` 拆分 19 个源文件并收紧基线至 `623` 后通过 |
| 仓库契约与符号校验 | `.venv/bin/python scripts/check_repository_contracts.py` | **`1`** | `0` | 重刷冻结输出 Schema 后通过（`26 settings`、`26 source symbols verified`） |
| Python 3.9 宿主兼容门禁 | `.venv/bin/python scripts/check_host_python.py --smoke` | `0` | `0` | `Host Python 3.9 contract: 13 scripts checked` |
| Web 面板与协作房间构建 | `scripts/build_web_assets.py --check` / `build-room.mjs --check` / `check-ui-copy.mjs --check` | `0` | `0` | 静态产物无漂移，明确 UI 文案错误 `0` 处，Prettier 全通过 |
| Node 单元与空白符检查 | `node --test tests/*.mjs web/browser-extension/*.test.mjs` / `git diff --check` | `0` | `0` | Node 单测 `46 pass / 0 fail`，空白符与冲突标记检查通过 |
| 发布一致性与凭据扫描 | `.venv/bin/python scripts/check_release.py` | — | `0` | `passed: true`，扫描公开文件无凭据泄漏模式命中 |
| 本机开发依赖锁比对 | `.venv/bin/python scripts/check_dependencies.py` | **`1`** | **`1`** | 属本机 `.venv` 环境小版本落后于 `requirements-dev.txt`（`opentelemetry-api`、`playwright`、`typing-inspection`、`websockets`），非源码缺陷 |
| 本次涉及的 20 个测试文件 | `.venv/bin/python -m pytest -q <12 改 + 8 增>` | `0` | `0` | **`460 passed in 153.79s`**，`test_mcp_catalog_modes.py` 硬预算常量未调高放水（`core_lean = 29,129 B < 30,000 B`、`default_json = 94,676 B < 95,000 B`、`collab_lean = 30,918 B < 32,000 B`） |
| 诊断方案 §5.2 14 个回归文件 | `.venv/bin/python -m pytest -q <14 必跑回归>` | `0` | `0` | **`412 passed in 117.07s`**，0 失败 |

---

## 1. 首轮审查问题（P0-1 ~ P3-9）代码级闭环核验

| 首轮编号 | 原始问题摘要 | 复核状态 | 代码级核验结论与关联残留项 |
| --- | --- | :---: | --- |
| **P0-1** | 非读操作异步 `mark_running` 崩溃后变 `retryable` 重放写副作用 | **已彻底闭环** | `Agent._begin_operation`（`agent/runner.py:278-294`）仅允许同时满足 `TOOLS[tool].scope == "read"` 且 `workspace_probe.eligible(tool, args)` 的纯文件只读工具走内存态；其余全部 8 处入口（含带 `read_only=True` 的 `exec`）均在发 `started` 与执行前经 `await self.disk_call(self.journal.mark_running_db, id)` 完成落盘，再核对 `not_after`。 |
| **P1-1** | `devices.last_seen` 冻结导致滚动升级必败、任务存活恒 stale | **主干已修 / 存次生竞态** | 全仓展示、诊断与升级已改调 `runtime.device_last_seen()` 与 `device_presence`；但面板滚动升级存在新连接 2s 半挂载状态撕裂与旧连接尾帧污染（见 **P1-05**），`task_liveness` 存在同进程断连新鲜度阈值倒挂（见 **P2-13**）。 |
| **P1-2** | 自报 `read_only=True` 降级显式 `write`/`read_stable` 锁与本地独占 `task` | **已彻底闭环** | `claims_for`（`agent/resource_queue.py:510-556`）与 `is_read_only_exec`（`shared/scheduler_config.py:61-78`）严保本地 `task` 策略与显式 `resources` 模式，仅在未声明 `resources` 时省去隐式 `cwd` 写锁。 |
| **P1-3** | 已开始执行的读操作探针超时被 `not_after` 截断为 `0.0s` | **主路径闭环 / 存边界残留** | `Agent.execute` 调用 `workspace_probe.probe` 已不再传入 `not_after`，恢复完整 30s 预算；但竞争获槽后的二次 `_operation_root` 校验仍传无裕量 `not_after`（见 **P2-02**）。 |
| **P1-4 / P2-1 / P2-2** | 协作后台大事务连坐回滚、`wake_*` 悬空、探针 SQL 漂移 | **拆解主干 / 引入死循环** | 吊销与事件派发已解耦且 `CheckedConnection.savepoint()` 会截断回滚子步骤的 `after_commit` 回调；但 `events.py:_recovery_step` 外包顶层事务导致 `RoomContinuations.recover` 触发 `_rollback_only` 永久死循环（见 **P1-03**），且仍漏探 `coordination_work` 租约过期（见 **P2-12**）。 |
| **P2-3 / P2-5** | `online()` 在事件循环查库；`_classify_write` 对未知表 fail-open | **主路径已修 / 门卫短路** | 全仓 35 处 `online()` 已全部位于工作线程；但 `CheckedConnection._changed` 的窄化前缀正则短路了前导注释 SQL、CTE、`ALTER/DROP` 与显式 `security_write=True`（见 **P1-01**），且 `meta` 表漏触发安全纪元（见 **P1-02**）。 |
| **P2-4 / P2-6 / P2-7 / P2-8** | 终态缓存无界、`NORMAL` 掉电风险、滚动投递默认全开、控制队列满即断连 | **主路径闭环 / 存握手泄漏** | `terminal_statuses` 限 2,048 条且不影响 SQLite 持久防重放；Hub 与 Agent 默认恢复 `WAL + FULL`；`delivery_rolling_devices` 默认置空；回执改用 `enqueue_reply` 30s 有界排队（但握手早退漏关 writer 见 **P1-06**，异步取消落盘异常丢批见 **P1-09**）。 |
| **P3-1 ~ P3-9** | 测试无界等待、`_wait` 冗余分支、远端并发、LSP 排队可见、游标校验、冻结字典 `\|=`、legacy `view=2` | **主体已落地** | 远端网关 `_effective_concurrency`、`search` 的 `resume_scan_scope`、`_FrozenDict.__ior__`、unified 下 legacy 工具重算 `presentation_options` 均已修复到位；`_FrozenList` 漏拦 `+=`/`*=` 见 **P3-10**。 |

---

## 2. 新发现与残留的 P1 级阻断缺陷（共 10 项）

| 编号 | 模块与精确行号（`edd5540c`） | 一句话根因摘要 |
| --- | --- | --- |
| **P1-01** | `hub/store_contract.py:153-155`；`hub/store.py:270-330, 884-893, 984` | `CheckedConnection._changed` 前置正则短路，使前导注释 SQL、`WITH` CTE、DDL、`executescript` 及显式 `security_write=True` 绕过 `_security_write`（Fail-Open） |
| **P1-02** | `hub/store.py:106-112, 984-1002`；`hub/mcp.py:465-481` | `_table_write_kind("meta")` 恒判非安全写且无纪元触发器，修改 `public_url` 等安全配置无法失效 OAuth 受众与证据缓存 |
| **P1-03** | `hub/collaboration/events.py:1894-1913`；`hub/store_contract.py:175-187` | `_recovery_step` 外包顶层事务毒化 `RoomContinuations.recover` 的内部 `DevError` 隔离，单条业务异常即整批回滚并每 5s 死循环锁死队头 |
| **P1-04** | `hub/tool_router.py:28, 46-86, 170`；`hub/mcp.py:613` | `ToolRouter._collision_cache`（`OrderedDict`）在 `run_read` 多线程池中无锁并发 `move_to_end` / `popitem`，存在链表损坏与 `KeyError` 竞态 |
| **P1-05** | `hub/panel_agent_rollout.py:351-391`；`hub/runtime.py:4143-4160` | 面板滚动升级存在“新连接挂载至入库 2s 状态撕裂”与“旧连接退出前尾帧刷 `last_seen`”两类竞态，会将正常升级节点误判为 `failed` |
| **P1-06** | `hub/runtime.py:206-223, 777, 4020, 4221` | `agent_socket` 握手限流发 `going_away` 或挂载前早退时，`detach()` 因连接未入表返回 `False` 未调 `connection.close()`，永久泄漏 `hub-ws-writer` 协程 |
| **P1-07** | `agent/lsp_navigation.py:157, 493-519, 769, 820`；`agent/runner.py:2766-2777` | `Peer.close` / `_close_entries` 未屏蔽 `CancelledError` 导致取消时跳过强杀泄漏进程与管道 FD；且未使用 `OwnedProcess` 导致同会话组孙进程泄漏 |
| **P1-08** | `agent/lsp_navigation.py:901-918`；`agent/runner.py:801-815` | 温池复用（`reused=True`）期间执行 `cancel_call` 或停机杀进程触发 `LSP_DISCONNECTED` 时，未校验取消/停机标志，反而冷启动新语言服务重跑整次查询 |
| **P1-09** | `agent/runner.py:254-273, 797-799`；`agent/journal.py:168-179` | `_drain_journal_persist` 遇瞬时 SQLite/IO 异常整批丢弃已出队的 `cancel_db`；60s 后 `journal.prune` 全表差集反推将内存 `cancelled_ids` 栅栏一并抹除 |
| **P1-10** | `shared/token_estimate.py:108-125, 361-395`；`shared/token_cost.py:76` | 顶层 `estimated_tokens` 改为未去重的 `wire_metric` 致 `mirror="full"` 计费翻倍；且 `Projection.text()` 对 `>256 KiB` JSON 字符串直接计 0 Token |

### P1-01 `CheckedConnection._changed` 前置正则拦截导致写分类器对 CTE/注释/DDL 失效（Fail-Open）

- **定位**：`hub/store_contract.py:153-155`、`hub/store.py:270-330, 884-893, 984`
- **触发路径**：`Store._classify_write(sql)` 专门实现了剥离 `--` / `/* */` 注释并对 `WITH`（CTE）、`ALTER`、`DROP` 等复杂语句返回 `(True, False)` 的保守失效逻辑；但真实数据库执行入口 `CheckedConnection._changed(sql)` 仅在 `re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I)` 命中时才回调 `self._on_write(sql)`（即 `Store._security_write`）。一旦写语句带前导注释（`-- ...\nUPDATE ...`）、使用 `WITH cte AS (...) UPDATE/DELETE/INSERT ...`、执行 `ALTER`/`DROP` 或调用 `executescript()`，`_security_write` 根本不会被调用，连显式传入的 `store.execute(..., security_write=True)`（`hub/store.py:884-893`）也会被静默吞掉，`session_revision` 与 `config_revision` 保持不变。
- **修复建议**：移除 `CheckedConnection._changed` 中的窄化前缀正则，改为对所有非纯 `SELECT` / `PRAGMA` / 事务控制语句统一回调 `self._on_write(sql)`（复用 `_tokenize_sql` 首词判断），并在 `executescript()` 成功后无条件推进 `session_revision` 与 `config_revision`。

### P1-02 `_table_write_kind("meta")` 一刀切判为非安全写，OAuth 受众变更无法失效鉴权快照

- **定位**：`hub/store.py:106-112, 984-1002`（影响 `hub/mcp.py:465-481`、`hub/connection_evidence.py:91-107, 145-148`）
- **触发路径**：`_table_write_kind("meta")` 固定返回 `(False, True)`，仅递增 `config_revision`，既不递增 `session_revision` 也无 `security_epoch` 触发器，导致 `store.epoch_stamp()` 完全不变。但管理员修改 `meta` 中的 `public_url`（`hub/api/settings.py:325`）会直接改变 `store.oauth_resource()`（`hub/oauth.py:45-46, 65`），按协议绑定旧受众的 OAuth `access` Token 必须立即以 `401 INVALID_TOKEN` 失效；由于 `epoch_stamp()` 未变，`hub/mcp.py:465-470` 的 `_revalidate_principal(refresh_only=True)` 命中 `stamp == auth_stamp` 直接跳过重验。同理，更新 `meta` 中的 `grant_projects_revision:*` 也不会失效 `ConnectionEvidence._observed_lru`。
- **修复建议**：在 `meta` 表上安装条件触发器 `WHEN NEW.key != 'security_epoch'` 推进 `security_epoch` 并在 `_security_write` 中同步递增 `session_revision`（后台非安全游标可通过显式 `security_write=False` 豁免），或将 `config_revision` 纳入 `epoch_stamp()`。

### P1-03 `events.py:_recovery_step` 外包顶层事务触发 `_rollback_only` 毒化，锁死恢复队头

- **定位**：`hub/collaboration/events.py:1894-1913`（关联 `hub/collaboration/room_continuations.py:658-703`、`hub/store_contract.py:175-187`）
- **触发路径**：`RoomContinuations.recover()` 内部按条运行 `try: with self.store.transaction(): ... except DevError as exc: with self.store.transaction(): UPDATE room_continuation_items ...`，依赖常规业务 `DevError`（如成员离线 `ROOM_RECOVERY_MEMBER_UNAVAILABLE`、证据变更 `ROOM_RECOVERY_EVIDENCE_CHANGED`）将不可继续的条目标记为 `retry_at = now + 30` 或 `state = 'skipped'`。`_recovery_step` 在外层多包了一层 `with tx():`（进入时 `depth == 1`）：一旦同批 8 条中任意一条抛出业务 `DevError`，内层 `with self.store.transaction():`（`depth: 2 -> 1`）退出时会将连接置为 `self._rollback_only = True`。即使 `RoomContinuations.recover` 捕获异常并执行了 `except` 更新，外层 `_recovery_step` 退出（`depth: 1 -> 0`）时仍会强制回滚整批事务（含已成功条目及异常条目的 `skipped`/`retry_at` 更新）并抛出 `TransactionAborted`，使异常条目永久滞留队首，每 5 秒重演整批回滚。
- **修复建议**：删除 `hub/collaboration/events.py:1896-1897` 外层的 `with tx():`，直接执行 `return callback()`（6 个恢复器内部均已独立管理顶层 `store.transaction()`）。

### P1-04 `ToolRouter._collision_cache` 在多线程只读池下无锁并发修改 `OrderedDict`

- **定位**：`hub/tool_router.py:28, 46-86, 170`；调用入口 `hub/mcp.py:613`
- **触发路径**：`hub/mcp.py:613` 将 `tools.resolve` 改由 `await run_read(tools.resolve, principal, name)` 在 `Store._read_executor`（3 个 OS 工作线程）中并发执行，而路由闭包内的 `ToolRouter` 实例全局共享。每次调用 `query` 或 `act` 时，`_check_unified_collision()` 在无任何线程锁保护下对 `self._collision_cache`（`OrderedDict`）并发执行 `move_to_end`、键值写入与 `popitem(last=False)`，同时无锁更新 `_native_names_cache`，并发穿过 `self.gateway.tools(principal)` 时会触发 `OrderedDict` 内部双向链表损坏或 `KeyError`。
- **修复建议**：在 `ToolRouter.__init__` 中增加 `self._cache_lock = threading.Lock()`，保护 `_collision_cache` 与 `_native_names_cache` 的读写与 LRU 移动（将 `self.gateway.tools(principal)` 保持在锁外）。

### P1-05 面板滚动升级 `last_seen` 判据存在半挂载撕裂与旧连接尾帧污染，误判升级失败

- **定位**：`hub/panel_agent_rollout.py:351-391`、`hub/runtime.py:4143-4160`
- **触发路径**：
  1. **新连接半挂载状态撕裂**：`agent_socket` 在 `hub/runtime.py:4143` 先执行 `_attach_connection` 将新连接写入 `self.connections[device_id]`（刷新内存 `last_seen` 且 `online()==True`），随后等待旧 socket 关闭与鉴权校验，直到 `4158` 行才由 `_register_agent` 更新 `devices.info`。在这最长约 2 秒的窗口内，若 `PanelAgentRollouts.tick()` 巡检该节点，会同时读到新连接的 `online=True`、`last_seen > op["updated"]` 以及尚未刷新的旧 `devices.info`（旧版本号或历史残留 `management.last_error`），直接命中失败分支将正在成功重连的节点误判为 `failed`。
  2. **旧连接退出前尾帧污染**：`agent_update` 完成写入 `op["updated"]` 后、Agent 收到 `ack` 退出前，旧连接上任何在途 `heartbeat`/`output`/`native_sync` 帧都会把 `connection.last_seen` 刷到 `> op["updated"]`，若有历史 `last_error` 则新进程尚未启动即被判败。
- **修复建议**：在 `Connection` 上记录完成 `_register_agent` 同事务落盘后的 `registered_at` 与该连接上报的 `info`，滚动升级仅以 `connection.registered_at > op["updated"]` 及当前连接自身 `info` 判定重连成功或失败。

### P1-06 `agent_socket` 握手限流或挂载前早退漏关 `Connection`，永久泄漏 `hub-ws-writer` 协程

- **定位**：`hub/runtime.py:206-223, 777, 4020, 4221`
- **触发路径**：`_admit_agent_handshake` 触发令牌桶限流调用 `await connection.send({"type": "going_away", ...})`（`4020` 行），或发送 `ready` 后在挂载前因 `stopping`/鉴权未过提前 `return` 时，`connection.send()` 已通过 `_ensure_writer()` 创建了阻塞在 `await self._queue_event.wait()` 上的后台 `hub-ws-writer` Task。进入 `finally` 调用 `await self.detach(device_id, connection)`（`4221` 行）时，因连接从未挂载进 `self.connections[device_id]`（或已被后续连接替换），`_detach_fence` 直接返回 `False` 而未调用 `connection.close()`，导致每次被限流或中断的握手都永久泄漏一个挂起的 `Task`、`Connection` 及两个 512 槽队列。
- **修复建议**：在 `agent_socket` 的 `finally` 块中，无论 `self.detach(device_id, connection)` 返回真假，只要 `connection is not None` 均无条件执行 `connection.close()`。

### P1-07 `LspPool` 清理路径未防 `CancelledError` 泄漏活进程与管道，且缺 `OwnedProcess` 会话组回收

- **定位**：`agent/lsp_navigation.py:157, 493-519, 688, 769, 820`、`agent/runner.py:2766-2777`
- **触发路径**：
  1. `_reaper_loop` 将过期 `_PoolEntry` 弹出 `self._entries` 后调用 `await self._close_entries(to_close)`；`_PoolEntry.close()` 先置 `self.peer = None`，再在 `Peer.close()` 中等待 1 秒 `shutdown` RPC。若此时 `shutdown_all()` 取消 `_reaper_loop` 或外层查询超时取消 `entry.close()`，`asyncio.CancelledError` 不会被 `contextlib.suppress(Exception)` 捕获，直接中断 `Peer.close()` 与 `_close_entries` 循环，跳过后续 `kill_process`、`reader.cancel()`、`stderr.cancel()` 及剩余待关实例，永久泄漏语言服务子进程与三路管道 FD。
  2. `Peer.start()` 使用原生 `create_subprocess_exec(..., start_new_session=True)` 而非 `OwnedProcess`。当 leader 正常退出（`process.returncode is not None`）后，`Agent.kill_process` 对非 `OwnedProcess` 实例直接早退（忽略 `include_finished=True`），无法清理 `rust-analyzer`/`gopls`/`tsserver` 派生的同会话组孙进程。
- **修复建议**：在 `Peer.close()` 中使用 `try...finally` 配合 `resource_queue.finish_cleanup` 屏蔽外部取消直至完成进程强杀、管道关闭与 `agent.processes` 清理后再重抛 `CancelledError`；`_close_entries` 保证遍历关闭全部条目；为 `Peer` 接入与 `OwnedProcess` 一致的进程组清理机制。

### P1-08 温池 `Peer` 被取消或停机强杀时误触发 `LSP_DISCONNECTED` 自动冷启重跑

- **定位**：`agent/lsp_navigation.py:901-918`、`agent/runner.py:801-815`
- **触发路径**：查询命中温池实例（`reused = True`）并登记 `agent.processes[identifier] = entry.peer.process`。当用户取消该查询（`Agent.cancel_call` 对 `running` 操作强杀 `self.processes[identifier]`）或 Agent 停机关闭池内进程时，`Peer.read_loop` 因管道断开将挂起请求置为 `DevError("LSP_DISCONNECTED")`。`_run_peer_request` 捕获 `reused and exc.code == "LSP_DISCONNECTED"` 时未检查操作取消或停机标志，立刻调用 `_spawn_peer` 拉起全新语言服务进程重跑整次查询，违背取消语义并在停机期间逆向派生新子进程。
- **修复建议**：在 `LSP_DISCONNECTED` 重试分支前校验 `identifier in getattr(agent, "cancelled", ()) or agent.journal.is_cancelled(identifier) or agent.stop_event.is_set()`，命中则直接抛出 `DevError("CANCELLED", "语义查询已取消", 409)`。

### P1-09 `_drain_journal_persist` 异常整批丢弃导致先到的 `cancel` 持久栅栏丢失并被 `prune` 抹除

- **定位**：`agent/runner.py:254-273, 797-799`、`agent/journal.py:168-179`
- **触发路径**：`cancel_call` / `cancel_pending_call` 仅写内存 `cancel_memory` 并将落盘任务推入 `_journal_persist_queue`。`_drain_journal_persist` 先用 `popleft()` 取出最多 128 条再执行 `disk_call(apply)`；一旦遇瞬时 `sqlite3.Error` 或 `OSError`，`except` 分支仅记录 `warning` 即丢弃整批已出队项。在 Hub 控制队列抢先送达 `cancel_pending`（早于 `call`）场景下：① 若此时 Agent 重启，SQLite `cancellations` 表缺失该栅栏；② 即便不重启，60 秒后只要 `journal.prune()` 删除过任意 7 天前旧记录，`self.cancelled_ids - current` 差集同步会因该 ID 不在库表也不在 `active_states` 中而将内存 `cancelled_ids` 里的栅栏一并 `discard`。
- **修复建议**：在 `_drain_journal_persist` 中按单条 `try/except` 隔离，并将失败的 `cancel` 与 `mark_running` 项通过 `appendleft` 退回队列带退避重试（或对 `_cancel_pending_frame` 直接 `await self.disk_call(self.journal.cancel_db, id, at)`）；`Journal.prune` 改为由 `journal_retention.prune` 返回当次实际过期的 ID 列表精确剔除，不再用全表差集反推。

### P1-10 `token_estimate.py` 将主计量改为 `wire_metric` 致 `mirror="full"` 计费翻倍，且 `>256 KiB` JSON 归零

- **定位**：`shared/token_estimate.py:29-38, 108-125, 361-395`（关联 `shared/token_cost.py:76-90`、`hub/mcp.py:760-771`）
- **触发路径**：
  1. **`mirror="full"` 翻倍计费**：`estimate_output()` 将返回字典的主体从去重后的语义投影 `sem_metric` 改为了 `wire_metric`（合并了 `content[0]`、`structuredContent` 与 `_meta`）。而 `token_cost.py:76` 计算参考成本与 `summarize()` 汇总均读取顶层 `estimated_tokens`，导致所有 `mirror="full"` 请求（legacy 工具默认 `view=2`、`profile=core/coding/full`、`verbosity=full`、`tasks/get`）被重复计费约 2 倍。
  2. **超长 `mirror="meta"` JSON 计为 ~0 Token**：当协作工具在默认 `view=3, mirror=meta` 下返回超过 `MAX_CHARACTERS`（262,144 字符）的 JSON 文本时，`_is_meta_stub` 为真使估算转入 `Projection.text(block["text"])`；而 `Projection.text()` 遇到 `startswith(("{", "[")) and len(text) > MAX_CHARACTERS` 直接置 `truncated = True` 并空返，导致 256 KiB 以上的正文计为 **0 Token**，整条响应仅计入几十字节的 meta stub（约 10 Token）；无 `structuredContent` 的远端网关工具超长 JSON 更是直接计为 `0`。
  3. **输入 `"files"` 排除丢失**：`EXCLUDED_KEYS` 移除了 `"files"`，使输入侧携带短附件文本时不再被排除。
- **修复建议**：顶层字典恢复以去重后的 `sem_metric` 为主口径，仅将 `wire_metric["estimated_tokens"]` 写入 `wire_tokens_est`；对无法走 `structuredContent` 的超长 JSON 文本截取前 `MAX_CHARACTERS` 前缀做正则脱敏后按字符权重计入 `partial=True` 估算；在 `_excluded()` 中仅对 `self.output` 放行 `"files"`。

---

## 3. P2 级中风险隐患详述（共 15 项）

### 3.1 Agent 执行、LSP 常驻池与文件系统层（P2-01 ~ P2-05）

- **P2-01 `_begin_operation` 落盘与内存态更新跨 `await` 分离，优雅停机窗口误产 `INTERRUPTED`**（`agent/runner.py:285-288, 1607, 2985`；`agent/journal.py:113-125, 261`）：`mark_running_db` 在工作线程提交 `status='running'` 后、事件循环恢复调用 `mark_running_memory` 前，`fast_status(id)` 仍为 `"accepted"`；此时触发优雅停机取消会跳过 `journal.finish`，下次启动把从未执行副作用的操作刷为 `INTERRUPTED`。应在 `Journal.mark_running_db` 事务成功后持 `_state_lock` 同步更新 `active_states`。
- **P2-02 竞争获槽后 `_operation_root` 二次校验超期未转译 `QUEUE_EXPIRED` 并误杀池化 `ReadWorker`**（`agent/runner.py:1197, 1569`；`agent/workspace_probe.py:287-295`）：获槽后临界跨过 `not_after` 会算出 `0.0s` 超时，误抛 `PROJECT_DISCOVERY_TIMEOUT` 并将刚租出的健康 `ReadWorker` 标为 `failed=True` 强杀；且 `ReadWorker.abandon_admission` 池化分支漏捕 `TimeoutError` 转译 `DevError`。
- **P2-03 常驻 `Peer.notifications > 20000` 累计上限未按查询重置，且同步 `lstat` 阻塞事件循环**（`agent/lsp_navigation.py:118, 283, 459`）：300 秒常驻期内通知计数从不清零，长期活跃必爆 `LSP_LIMIT`；`Peer.documents` / `diagnostics` 无界增长，且每次查询在事件循环线程同步对全部历史打开文档调 `_uri_to_path` 与 `lstat()`。应按次重置通知计数、限制打开文档 LRU 上限并将磁盘状态检查移入 `disk_call`。
- **P2-04 `Peer.send` 缺写锁并发 `stdin.drain()` 抛 `AssertionError`，推送诊断未校验文档版本**（`agent/lsp_navigation.py:227, 286, 348, 1058`）：主查询同步大文件等待 `stdin.drain()` 时，若 `read_loop` 回复服务端 `server_request` 并发调用 `stdin.drain()`，CPython `StreamWriter` 直接抛 `AssertionError`；且推送式诊断分支仅判 `while uri not in peer.diagnostics`，会被在途旧版本通知提前唤醒或命中未失效的依赖缓存。应为 `Peer.send` 加 `asyncio.Lock` 并对齐 `version == doc_version` 等待条件。
- **P2-05 `FileEngine.search` 的 `offset` 与 `resume_after_path` 双游标耦合**（`agent/filesystem.py:1050-1118`）：`offset > 0` 翻页后遇 `scan_limited` 若客户端保留原 `offset` 会静默跳过新起点前 `offset` 条命中；`resume_after_path` 续扫中遇 `page_full` 时 `hint` 漏提示继续携带 `resume_after_path`；0 文件超限时 `last_scanned_path` 为 `None` 会丢失传入的 `resume_after_path`（应回退为 `last_scanned_path or resume_after`）。

### 3.2 Hub 存储锁、连接证据与运行时层（P2-06 ~ P2-10）

- **P2-06 `_SECURITY_TABLES` 漏标房间状态与多张凭证表，`integration_admission` 误入 `_SNAPSHOT_TABLES`**（`hub/store.py:25-103`；对照 `hub/room_scope.py:255-272`、`hub/iam_schema.py:15-83`）：`effective_grant` 直接查询 `collaboration_rooms` 与 `conversation_rooms` 判定房间级授权是否仍 `active`，但二者及 `native_ownership`、`space_invites`、`oauth_requests/codes`、`room_host_control_handles` 等表均无 `cp_epoch_*` 触发器；且项目暂停开关表 `integration_admission` 被列入 `_SNAPSHOT_TABLES`，暂停变更不推进 `session_revision`。建议补齐表分类并增加枚举 `sqlite_master` 全表显式归类的门禁单测。
- **P2-07 `_evidence_contexts` 仅按 `session_revision` 缓存 `principal`，漏检自然时间过期且回退分支恒空**（`hub/runtime.py:721, 3602`；`hub/connection_evidence.py:188-193, 324-354`）：`grant_expirations`、`memberships.expires` 等基于时间自然过期不产生写库，`session_revision` 不变，长读操作完成后仍把已过期凭据记为有效证据；且缓存淘汰未命中时 `operations.payload` 已在 `3602` 行先置为 `NULL`，导致解密回退分支永远拿到空值。
- **P2-08 删除设备后 `device_presence` 孤儿记录复活，重连初期暴露重启前旧调度快照**（`hub/store.py:335`、`hub/api/devices.py:203-207`、`hub/runtime.py:714, 790`、`hub/scheduler_settings.py:100-103`）：`delete_device` 先删 `devices` 再调 `disconnect_device`，触发 `_upsert_device_presence` 重新插入已删设备记录；且 `COALESCE(excluded.runtime_info, device_presence.runtime_info)` 使 Agent 重启重连后的前 12 秒内回退读到旧进程的负载/内存告警快照。
- **P2-09 `HubIntegrations.finish` 节流了 `mcp_activity` 清理，但遗漏内层 `MCPUsage.record -> prune()`**（`hub/integrations.py:387-436`、`hub/mcp_usage.py:76`）：外层每 500 次或 300s 才清理一次 `mcp_activity`，但同事务内调用的 `MCPUsage.record` 仍每次无条件跑两条重型 `DELETE` 占用唯一写线程；且 `_finishes_since_prune = 0` 在事务提交前重置，回滚会跳过清理。
- **P2-10 `going_away` 经控制队列抢占越过待发 `call` 帧；`SecureChannel` 同步/异步混用无锁检测**（`hub/runtime.py:127, 4244`、`shared/crypto.py:173-208`）：Hub 停机广播 `going_away` 会越过 `_data_queue` 中排队的 `call` 先送达 Agent，随后 `_writer_loop` 继续把旧 `call` 发给准备断开的 Agent；`SecureChannel.pack/unpack` 未检测 `_pack_lock/_unpack_lock` 是否已被异步协程持有。

### 3.3 Hub 协作生命周期、远端网关与 MCP 流层（P2-11 ~ P2-15）

- **P2-11 `_run_reconcile` 外包事务合并导致回滚时内存游标漂移，且恢复异常不反映至组件健康度**（`hub/collaboration/lifecycle.py:254-274, 378`、`hub/collaboration/service.py:1991-2065`、`events.py:1894-1902`）：`service.reconcile()` 内部原本拆为 3 段独立事务，被外层 `with store.transaction():` 重新合并后，第 3 段回滚不会回退前两段已推进的内存游标 `_settle_after` 与 `_reconcile_after`；且 `_recovery_step` 吞掉异常使 `service.health["events"]` 恒显示 `"running"`。
- **P2-12 `_probe_reconcile` 漏探 `coordination_work` 租约到期，`wake_events` 被 20s 门控屏蔽**（`hub/collaboration/lifecycle.py:215-249`、`hub/collaboration/events.py:968, 1879-1890, 1944`）：`_probe_reconcile` 未查 `coordination_work` 的过期 `lease_until`（退化至 60–80s 兜底）；空闲期触发 `wake_events()` 时因 `_relay_recover_after` 未重置且未纳入 `next_dispatch_delay()`，房间解暂停恢复最长延迟达 39s；`grant_expirations` 缩短触发器与成员撤销路径亦漏接 `wake_renewals` / `wake_reconcile`。
- **P2-13 `task_liveness` 同进程节点断连后新鲜度阈值从 45s 反向放宽至 75s**（`hub/collaboration/task_liveness.py:22-23, 292-301`）：挂载了 `runtime.connections` 的主进程内，`live_contact is None` 表示节点此刻已断连（断连时 `_record_disconnect` 已将最终接触时间刷盘），将阈值放宽到 75s 会使刚超时断连的 `stale` 节点在断连后 30 秒内倒挂变回 `fresh`。应按 `runtime.connections` 是否挂载而非单个设备是否在线选择 45s/75s。
- **P2-14 `gateway/remote.py:Session.close()` 未等待锁外进行中的 `initialize()` 握手排空**（`hub/gateway/remote.py:105-120, 155-190`）：慢握手期间 `self.initializing == True` 且 `self.active == 0`，`close()` 仅等待 `not self.active` 即提前返回且 `should_close` 为 `False`，遗留未关闭的后台握手连接。应改为等待 `not self.active and not self.initializing`。
- **P2-15 `mcp_streams.py` 客户端断连在生成器 `finally` 提前释放并发配额与同 ID 防重锁**（`hub/mcp_streams.py:134, 189`）：客户端断开 SSE 时生成器立即调用 `_release_key(key, stop)`，但底层后台 `work` 协程可能仍在运行，反复“升级 SSE → 断连重发”可绕过 `per_principal_limit` 与 `DUPLICATE_REQUEST` 堆积后台任务。应仅在 `work` 的 `done_callback` 中释放 `key`，并为 `StreamingResponse` 补 `BackgroundTask(stop.set)`。

---

## 4. P3 级改进与观察项汇总（共 11 项）

1. **P3-01 毒丸读早崩绕过 3 次熔断**（`agent/runner.py:278`；`agent/journal.py:72, 88-107`）：eligible `read` 工具在异步 `mark_running_db` 落盘前若令进程崩溃，重启时状态仍为 `accepted -> retryable`，`recovery_attempts` 不递增。可在 `retryable` 重放启动时同步落盘一次 `running`。
2. **P3-02 同步入口 `cancel_pending_call` 冷终态误判**（`agent/runner.py:782`）：直接调用同步方法时对超过 2,048 条 LRU 淘汰的冷终态返回 `"missing"` 并写入冗余取消记录；建议收口为内部私有辅助并统一走异步 `_cancel_pending_frame`。
3. **P3-03 `RESOURCE_BUSY` 丢弃调度器 `queue_reason`**（`agent/scheduler.py:386-401`；`agent/runner.py:1641-1646`）：`_abort_waiting` 计算了容量排队 `reason`，但 `runner.py` 组装错误响应时仅透传了 `blocked_by`；且有 `blockers` 时 `reason` 应归一为 `"resource_conflict"`。
4. **P3-04 `AgentTelemetry.starts` 跨线程无锁访问与停机未 `flush`**（`agent/runner.py:364, 1607`；`agent/telemetry.py:80`）：事件循环线程无锁 `starts.pop` 与 `disk_call` 线程持 `_mem_lock` 修改同一 `OrderedDict` 并发交错；`Agent.run` 退出关闭 `journal.db` 前应补调 `disk_call(telemetry.flush)`。
5. **P3-05 `tree(depth=None)` 字符串切分异常与 `LspPool` 目录重建隔离**（`agent/filesystem.py:697`；`agent/lsp_navigation.py:1218`）：`git` 分支 `split("/", depth)` 在 `depth is None` 时会抛 `TypeError`；`LspPool` 池键建议纳入根目录 `(st_dev, st_ino)` 防止删建同名目录复用旧实例。
6. **P3-06 `enqueue_reply` 30s 超时低于 16MiB 帧最大 42s 发送上限**（`hub/runtime.py:132, 289, 4202`）：慢速链路上合法 16MiB 大帧（超时 `10 + 16*2 = 42s`）叠加 >512 个小回执会在 30s 提前断连，建议调至 `>= 45s`；且 `WebSocketDisconnect` 分支中的 `flush_control` 因对端已断恒空返。
7. **P3-07 `computer_status(probe=True)` 已投递取消门禁未复用参数敏感判定**（`hub/runtime.py:2074, 2110`）：`2110` 行判断 `op["tool"] not in PROCESS_TOOLS | CANCELLABLE_READ_TOOLS` 而非 `not is_cancellable_read`，使 `probe=True`（`computer` 作用域）已投递操作绕过 `NOT_CANCELLABLE`。
8. **P3-08 `_exceeds_json_threshold` 对大容器先全量压栈再检查阈值**（`shared/crypto.py:30-43`）：超大 `list`/`dict` 在压栈全部元素后才判断 `approx > threshold`，应在压栈前先计入容器长度做早退检查。
9. **P3-09 `ConnectionEvidence` 环境变量缺有限数校验且残留死字段**（`hub/connection_evidence.py:55-61, 76, 221`）：`CODEPIER_EVIDENCE_DEDUP_SECONDS` 直接调 `float()` 未拦 `NaN/inf`（应改用 `env_float`），且 `_authentication_observed` 只写不读。
10. **P3-10 `_FrozenList` 漏拦 `+=` 与 `*=` 原地修改**（`shared/contracts.py:1709-1728`）：CPython `list.__iadd__` / `__imul__` 不走 `extend`，应补 `__iadd__ = __imul__ = _readonly`，并在 `freeze_schema` 中递归转换 `tuple`。
11. **P3-11 `_METERING_EXECUTOR` 取消时跨线程并发写 `trace`**（`hub/mcp.py:61, 1076-1088`）：请求在 `await run_in_executor` 期间被取消时，后台线程修改 `trace["token_usage"]` 与外层收尾 `integrations.finish(trace)` 并发交错；建议线程池仅返回纯计算字典，由事件循环协程写回 `trace`。

---

## 5. 测试质量与发布卫生专项审计

### 5.1 4 处使用假桩短路真实链路的新增测试

1. **`tests/test_b2_b6_b7_b10_b18_agent_core.py:720-764`（`test_mutation_start_is_durable_before_started_and_execution`）**：旨在验证 P0-1 副作用操作预落盘，但构造请求时误传了 `read_only=True`，并用 `monkeypatch.setattr(instance, "execute", execute)` 替换了真实执行器；建议增加覆盖真实 `fs_write` 工具及崩溃重启后 Hub 收到 `interrupted` 拒绝重放的端到端测试。
2. **`tests/test_b4_b9_b12_mcp_layer.py:132-168`（`test_unified_tool_resolve_skips_schema_deepcopy_and_capture_output_off_writer`）**：将 `app.state.runtime.invoke` 替换为返回静态字典的 `fake_invoke`，导致对 `store.run` 零调用的断言只测到了 `hub/mcp.py` 路由外壳，绕过了 `Runtime.invoke -> _admit_operation` 真实链路。
3. **`tests/test_b4_b9_b12_mcp_layer.py:613-736`（`test_unified_wait_single_event_and_dispatch_lock_scoped`）**：同时打桩替换了 `runtime.wait_for_operation` 与 `runtime.invoke` 并用 `SimpleNamespace` 手工伪造 `plan`，跳过了 `unified_contracts.translate()` 与真实 `Future` 完成唤醒。
4. **`tests/test_b1_b14_b3_b8_b16_hub_core.py:72, 203-224`**：`L72` 用 `lambda` 短路了 `connection_authorized`；`L203` 回退默认 `FULL` 后函数名仍叫 `test_store_synchronous_normal_override...` 且内部变量 `store_full` 实际绑定的是 `NORMAL` 模式实例。

### 5.2 默认 `/mcp` 端点 `view=3` 结构变化与过程文档清理建议

- **默认响应结构变更须写入 `CHANGELOG.md`**：从 `tests/test_mcp_events_protocol_compat.py:132-135` 与 `tests/test_unified_tools_http.py:69-74` 可见，默认切至 `view=3, mirror=meta` 后，`query(action="project")` 的项目列表元素由对象简化为字符串，错误回执的 `structuredContent.recovery` 简化为 `recovery_summary`（需显式加 `?view=2` 才返回旧结构）。
- **移出误入公开源码清单的临时过程文档**：`45e6b49c` 将 `docs/MCP_UNCOMMITTED_CHANGES_REVIEW_2026-11-20.md` 与 `docs/MCP_UNCOMMITTED_CHANGES_FIXES_2026-11-20.md`（含大量已修复的过时故障断言与本机运行态临时目录结构描述）一并纳入了 `main` 与 `shared/source_inventory.json:63-65`。参照 `CONTRIBUTING.md:9` 与 `.gitignore:45-112` 对历次内部审查/修复记录的管理惯例，正式发版前应将临时工作便签移入 `docs/archive/`（或归入 `.gitignore` 并从 `git` 索引移除），随后运行 `scripts/update_source_inventory.py` 与 `scripts/check_release.py` 刷新公开源码清单。

---

## 6. 建议修复与发版推进顺序

1. **Wave A — 存储鉴权与协作死循环止血（P1-01 ~ P1-04）**：修复 `CheckedConnection._changed` 前缀正则短路、补齐 `meta` 与房间/凭证表的安全纪元失效、移除 `events.py:_recovery_step` 外层冗余事务、为 `ToolRouter` 补加 `threading.Lock`。
2. **Wave B — 连接生命周期与常驻 LSP/取消栅栏加固（P1-05 ~ P1-10）**：为 `Connection` 增加已注册时间戳修复滚动升级竞态、`agent_socket` `finally` 无条件 `connection.close()`、`Peer.close` 防取消泄漏并接入进程组回收、取消/停机不触发 LSP 冷启重试、`_drain_journal_persist` 失败退回与 `prune` 精确剔除、恢复 `token_estimate.py` 顶层语义口径与超长 JSON 前缀估算。
3. **Wave C — P2 收敛与真链路测试补强**：按模块清理 P2-01 ~ P2-15（重点处理 `mark_running_db` 原子内存更新、`Peer.send` 写锁与通知重置、`task_liveness` 阈值、`Session.close` 等待握手排空、`mcp_streams` 配额释放时机），并将 4 处假桩单测改造为真实链路断言。
4. **Wave D — 公开源码清单清理与全套门禁复验**：整理临时过程文档并重刷 `scripts/update_source_inventory.py`，复跑 `ruff`、`mypy`、`check_function_complexity.py`、`check_repository_contracts.py`、`check_release.py` 及全量回归集后发布新补丁版本。
