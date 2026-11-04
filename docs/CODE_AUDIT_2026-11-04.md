# CodePier 代码审计报告 · 2026-11-04

> 公开脱敏版；历史观察不代表当前环境，问题状态以当前代码与验证结果为准。
> 逐项核实、修复证据与剩余边界见[审计修复记录](CODE_AUDIT_REMEDIATION_2026-11-04.md)；原报告中的判断不自动代表核实后的结论。

> 范围：Hub / Agent / shared / scripts / web / deploy 全部源码
> 基准版本：GitHub `main` @ `d0d587c`（2026-11-04 18:42）。行号均以此版本为准。
> 结论均经源码核对；标注「已复现」的条目附有可运行脚本（见附录 A 与各节）。
> 2026-11-04 晚间补充：`hub/collaboration/*` 精读（P1 新增实例、P12、P16–P20）、本地检出落后 GitHub 的成因与 CodePier 侧优化（P13、P14）、老部署 OAuth 白名单缺 `claude.ai`（P15）。
> 第三轮（逐行通读协作模块 + 全库查询计划扫描）：消息翻页全表扫描（P21）、房间读取放大（P22）、附件配额全站共享（P23）、两条归档路径冲突（P24）、后台泵异常处理（P25）、只进不出的待处理集合（P26）、搜索误命中（P27）、微信回复静默丢弃（P28）。
> 第四轮（剩余协作文件逐行通读）：新增 P29–P32，并补充 P1、P12、P16、P20–P23、P26。

---

## 0. 先说结论

整体工程质量很高：没有 `shell=True`、`pickle`/`eval`、解压穿越、令牌非常量时间比较等常见硬伤；文件工具有逐段符号链接检查、`O_NOFOLLOW`、硬链接拒绝；OAuth 强制 S256 PKCE、刷新令牌轮换与重用撤销；HTTP 请求体有全局预算；容器非 root、只读根文件系统、依赖哈希锁定。

真正的问题集中在六类：

1. **事务语义被悄悄破坏**——`Store.execute()` 会把调用方的外层事务提前提交，多处注释中承诺的「原子写入」实际上不成立（已复现）。协作模块里最严重的一处，会让目标执行步骤在记账失败后仍被投递执行，且不受硬超时与预算约束。
2. **数据只增不减**——Agent 的 journal、文件备份、检查点，Hub 的操作结果与审计表，以及协作模块的大部分流水表都没有保留期清理；Hub 还把读取到的源码全文以明文永久存在数据库里。
3. **一条坏消息卡死整条外部渠道**——Telegram / 微信轮询按批提交，单条更新出错时整批回滚、游标不前进，后续所有消息被阻塞。
4. **AI 会看到旧代码，或「找不到」存在的代码**——隔离目录默认从本地 `HEAD` 起步、合并后无人收尾，主检出长期落后 GitHub；全项目搜索的预算被 `.venv-*`、`.work` 耗尽而返回 0 条（均已在你的 Mac 上实测）。
5. **几个边界缺口**——Hub→Agent 通道可被重放（已复现）；自定义配置路径时配置备份泄露设备密钥（已复现）；09-20 至 11-04 之间安装的部署，OAuth 白名单缺 `claude.ai`，Claude 无法连接。
6. **唯一的数据库线程被慢查询占满**——Hub 所有数据库操作串行跑在一个线程上，而聊天记录每翻一页要全表扫 4 张最大的表，每次房间操作要重算上百次完整授权，后台对账每秒重扫只进不出的集合（P21、P22、P26）。表越大越慢，最终表现为 MCP 调用整体变卡。

| 编号 | 级别 | 类别 | 位置 | 一句话 |
| --- | --- | --- | --- | --- |
| [P1](#p1) | 🔴 高 | 正确性 | `hub/store.py:191-202` | 事务内调用 `store.execute()`/`store.audit()` 会提前提交整个外层事务（355 处调用点） |
| [P2](#p2) | 🔴 高 | 运维 / 隐私 | `hub/runtime.py:1330,1366` | 操作结果（含源码全文、命令输出）明文写库，`operations`/`audit` 永不清理 |
| [P3](#p3) | 🔴 高 | 运维 | `agent/journal.py`、`agent/filesystem.py:710` | Agent 调用记录、文件备份、项目检查点全部无限增长 |
| [P4](#p4) | 🟠 中 | 安全 | `shared/crypto.py:43`、`agent/runner.py:290` | 会话密钥只取决于 Hub 的 challenge，录下的 Hub→Agent 流量可重放 |
| [P5](#p5) | 🟠 中 | 安全 | `agent/runtime_config.py:103`、`agent/filesystem.py:125` | 配置备份 `config.json.runtime-backup-*` 含设备密钥，可被 `read` 读出且永不清理 |
| [P6](#p6) | 🟠 中 | 性能 | `agent/runner.py:1211` 等 | Agent 在事件循环线程同步写 SQLite（每 250ms 写 128 KB + fsync） |
| [P7](#p7) | 🟡 低 | 安全 | `hub/auth.py:151` | 任意账号登录成功会清空该 IP 的全部失败计数，IP 限流可被绕过 |
| [P8](#p8) | 🟡 低 | 安全 | `shared/crypto.py:24` | scrypt 参数为 N=2¹⁴，低于当前推荐值，且无透明升级路径 |
| [P9](#p9) | 🟡 低 | 部署 | `deploy/k3s/deployment.yaml:48` | 信任整个 Pod 网段的 `X-Forwarded-For`，集群内任意 Pod 可伪造来源 IP |
| [P10](#p10) | 🟡 低 | 性能 | `hub/agent_install.py:340` 等 3 处 | async 函数内同步访问数据库，阻塞 Hub 事件循环 |
| [P11](#p11) | ⚪ 清理 | 可维护性 | 多处 | 死代码与冗余查询（每次投递多一次 `SELECT grants`、重复集合元素等） |
| [P12](#p12) | 🔴 高 | 可用性 | `hub/collaboration/telegram.py:424`、`weixin.py:370` | 渠道轮询整批提交：一条更新出错，整个 bot 的后续消息全部卡住，24 小时后丢失 |
| [P13](#p13) | 🔴 高 | 工作流 | `agent/workspaces.py`、`shared/integration_contracts.py:109` | 隔离目录默认基线是本地 `HEAD`、不 fetch、不跟踪合并、不收尾；主检出落后 109 个提交 |
| [P14](#p14) | 🟠 中 | 正确性 | `agent/filesystem.py:319-466` | 全项目搜索不遵守 `.gitignore`，预算耗在 `.venv-*` / `.work`，对存在的代码返回 0 条（实测） |
| [P15](#p15) | 🟠 中 | 部署 | `deploy/install-hub.sh:69`、`scripts/panel_update_runtime.py:502` | 老部署 `.env` 里写死旧白名单，`claude.ai` 回调被拒，Claude 连接失败 |
| [P16](#p16) | 🟠 中 | 性能 | `hub/collaboration/channel_media.py:306,376` | 附件（≤32 MB）双重 base64 后存进主库单行；每个 Range 请求都在数据库线程整文件解密 + 哈希 |
| [P17](#p17) | 🟡 低 | 性能 | `hub/collaboration/service.py:973`、`lifecycle.py:27` | 面板总览 N+1 查询；7 个后台循环每 1–5 秒占用唯一数据库线程 |
| [P18](#p18) | 🟡 低 | 运维 | `hub/collaboration/*` | 协作流水表无保留期；`telegram_inbox` 配额查询缺索引，随历史线性变慢 |
| [P19](#p19) | 🟡 低 | 正确性 | `hub/collaboration/coordination.py:1032` | 协作目标到期后永远停在 `active` |
| [P20](#p20) | ⚪ 清理 | 健壮性 | 10 处 `next(...)` | 名单不一致时抛 `StopIteration`，变成 500 而不是业务错误 |
| [P21](#p21) | 🔴 高 | 性能 | `chatroom.py:208`、`dot_relay.py:106`、`room_diagnostics.py:48`、`channel_router.py:185,682` | 聊天记录每翻一页全表扫 5 次；工作列表按目标重复全表扫，最多约 2000 次；新消息写事务里也有全表扫（20 万条实测 61.7 ms → 0.81 ms） |
| [P22](#p22) | 🟠 中 | 性能 | `room_runtime.py:632`、`rooms.py:243,357` | 每个房间写操作都返回完整快照，触发最多 1001 次完整授权；SSE 每 5 秒按连接重算 |
| [P23](#p23) | 🟠 中 | 可用性 | `channel_media.py:22`、`room_attachments.py:43` | 附件缓存 256 MiB 全站共享，任一用户上传 32 个文件即可让所有人 7 天内收不了附件 |
| [P24](#p24) | 🟠 中 | 正确性 | `rooms.py:419`、`conversation_archive.py:336` | 两条归档路径各做一半；交叉恢复直接 500，渠道消息被静默隔离 |
| [P25](#p25) | 🟠 中 | 健壮性 | `events.py:884,954`、`channel_router.py:577` | 投递泵只捕获少数异常，一条坏数据中断整轮并取消在途投递；镜像串行发送 |
| [P26](#p26) | 🟠 中 | 性能 | `dot_mailbox.py:554,714`、`channel_router.py:366`、`monitor.py:401` | 邮箱消息条目永不确认、`unknown` 镜像每秒重刷、监控证据永不删除——每轮全量重扫 |
| [P27](#p27) | 🟡 低 | 正确性 | `chatroom.py:608`、`conversations.py:340` | 搜索在 JSON 原文上 `LIKE`：搜字段名命中所有消息，含引号/换行的正文搜不到 |
| [P28](#p28) | 🟡 低 | 正确性 | `weixin_delivery.py:78` | 微信回复日上限把已送达的也算进去，超限后静默丢弃、面板无提示 |
| [P29](#p29) | 🟠 中 | 性能 | `relay_hints.py:11-40` | 实时提示触发器对 `operations` 的每次写入（含每段输出、心跳）全表扫房间并刷新整个项目的面板 |
| [P30](#p30) | 🟠 中 | 正确性 | `project_scopes.py:240-252` | 撤销项目授权会把已取消、已到期的目标改回「暂停」，之后可被恢复执行 |
| [P31](#p31) | 🟡 低 | 正确性 | `telegram_protocol.py:64`、`weixin_protocol.py:262` | 大文件外发用固定截止时间，慢上行时必然超时，结果永久停在「未知」 |
| [P32](#p32) | 🟡 低 | 滥用 | `channel_enrollment.py:242`、`telegram.py:596` | 配对窗口内任何陌生人每发一条消息，机器人都回一条提示 |

建议修复顺序：**P15 → P12 → P1 → P21 → P29 → P13/P14 → P23 → P24 → P25 → P30 → P4 → P5 → P3 → P2**，其余随维护版本处理（详见第 4 节）。

---

## 1. 审计范围与方法

### 1.1 覆盖情况

| 层面 | 方法 | 覆盖 |
| --- | --- | --- |
| 全部 Python（agent / hub / shared / scripts / deploy，约 9.0 万行） | ruff 扩展规则集：`F,E9,B,S,ASYNC,PLE,PLW,RUF006,RUF012,RUF013,BLE001,E722,DTZ,PERF,G,LOG`，共 910 条，逐条甄别高信号项 | 100% |
| 全部 Python | 自写 AST 扫描：① 事务块内的提前提交调用；② async 函数内的同步数据库访问 | 100% |
| 全部 Python | 危险模式检索：`shell=True`、`extractall`、`pickle/eval/exec`、令牌 `==` 比较、`yaml.load`、XML 解析 | 100% |
| 全部前端 JS（约 2.6 万行，不含 vendor） | `node --check` 语法检查；`innerHTML`/`insertAdjacentHTML` 插值是否经过 `E()`/`esc()`；`EventSource`/`setInterval` 的关闭配对 | 100% |
| 人工精读 | `hub/auth.py`、`hub/mcp.py`、`hub/http.py`、`hub/oauth.py`、`hub/store.py`、`hub/runtime.py`（握手、入队、完成、投递）、`shared/crypto.py`、`agent/filesystem.py`、`agent/journal.py`、`agent/runner.py`（握手、消息循环、exec 子进程）、`agent/runtime_config.py`、`agent/artifacts.py`、`agent/ssh.py`、`agent/skills.py`、`Dockerfile`、`compose.yml`、`install.sh` | 安全与数据路径核心 |
| 协作模块精读（补充） | `api.py`、`service.py`、`coordination.py`、`network.py`、`events.py`（订阅/验证/投递）、`telegram.py`（入站/批提交/轮询）、`weixin.py`（入站/批提交/轮询失败）、`channel_media.py`、`consumer_engine.py`、`lifecycle.py`、`retention.py` 全文；`team_collaboration.py`、`room_runtime.py`、`room_goal_*`、`chatroom.py` 关键路径 | 鉴权、租约/围栏、外部输入、出站网络、后台循环 |
| 协作模块全量扫描（补充） | 133 张表的删除路径映射；`next()` 无默认值、时钟源混用、吞异常等特征扫描；关键查询的 `EXPLAIN QUERY PLAN` | 全部 70 余个文件 |
| 工作副本（补充） | 在你的 Mac 上实测 `git worktree list`、主检出与 `origin/main` 差距、各 worktree 合并与脏状态、`.work` 体积；用 CodePier 自身的搜索复现假阴性 | — |
| 协作模块逐行通读（第三轮） | `rooms.py`、`events.py`、`dot_mailbox.py`、`room_consumers.py`、`room_runtime.py`、`delegation.py`、`conversation_archive.py`、`chatroom.py`、`conversations.py`（读取/变更流）、`weixin_delivery.py`、`weixin_protocol.py`、`weixin_qr.py`、`channel_router.py`、`channel_media_router.py`、`room_attachments.py`、`common.py`、`lifecycle.py`、`dot_relay.py`（回执）、`dot_chat.py`（入队/回执）、`task_liveness.py`（恢复）、`monitor.py`（评估/清理） | 新增 P21–P28 |
| 全库查询计划（第三轮） | 用 `Store` 真实迁移出的 441 个库对象建库，对 `hub/` 全部字面量 SQL 做 `EXPLAIN QUERY PLAN`，筛出对随历史增长的表的全表扫描；关键查询用 20 万行数据计时；修复用的索引逐条验证计划 | 全部字面量 SQL |
| 协作模块逐行通读（第四轮） | 第三轮后剩下的协作文件；对新发现的热点查询再次做 `EXPLAIN QUERY PLAN`；修复用的触发器与索引在真实库结构上编译验证 | 新增 P29–P32 |

下列协作文件在第三轮之前只做过机械扫描（ruff、两个 AST 扫描、查询计划扫描、`next()`/空行下标特征扫描），第四轮已逐行通读：`room_weixin_native.py`、`room_channels.py`、`channel_owner_authority.py`、`working_scopes.py`、`owner_task_context.py`、`room_context.py`、`room_goal_scopes.py`、`room_goal_handoffs.py`、`room_goal_recovery.py`、`message_handling.py`、`joining.py`、`dots.py`、`dot_consumers.py`、`delegation_consumer.py`、`project_scopes.py`、`channel_enrollment.py` 及 `channel_*` 设置类小文件、`room_migration.py`、`room_diagnostics.py`、`room_work_bridge.py`、`room_pending_bindings.py`、`room_connections.py`、`experience_projection.py`、`telegram_files.py`。其中 `dots.py`、`delegation_consumer.py`、`project_scopes.py`、`room_migration.py`、`room_diagnostics.py`、`experience_projection.py` 按函数抽读，重点看查询、状态转换和授权入口；`qr_image.py` 是纯 QR 编码，只核对了输入边界。这些文件大量调用 `store.execute()` 与嵌套 `transaction()`，P1 修复后应结合回归测试重点复核。

### 1.2 关于工作副本

本报告行号以 GitHub `main` 为准；所列问题已确认在 Mac 工作副本中同样存在。工作副本为什么会落后、CodePier 应如何改进、以及现在如何安全地同步，见 [P13](#p13)。

---

## 2. 问题详情

<a id="p1"></a>
### P1 🔴 事务内的 `store.execute()` 会提前提交外层事务

**位置**：`hub/store.py:191-202`

```python
@contextmanager
def transaction(self, *, immediate=True):
    """Join the checked outer context; any nested failure is rollback-only."""
    with self.lock, self.db:
        if not self.db.in_transaction:
            self.db.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
        yield self.db

def execute(self, sql: str, args=()):
    with self.lock, self.db:          # ← sqlite3.Connection.__exit__ 会 COMMIT
        return self.db.execute(sql, args)
```

`sqlite3.Connection` 作为上下文管理器退出时会对 **当前连接上整个未结束的事务** 执行 `COMMIT`。因此只要在 `with store.transaction():`（或 `with store.lock, store.db:`）内部调用 `store.execute()`、`store.audit()`（默认 `commit=True`），或者嵌套调用 `store.transaction()`，外层事务就会在这一刻被提交；之后的语句运行在新的隐式事务里，外层再失败也回滚不了前面的写入。

**实际后果（举例）**

- `hub/runtime.py:1362-1376` `Runtime.complete()`：注释写明「终态结果与完成提示原子提交；出盒写入失败时原操作必须保持可恢复」。实际上 `UPDATE operations SET state=...` 走的是 `store.execute()`，在 `events.operation_completed()` 写出盒之前就已提交。出盒失败时，操作已是终态，订阅方永远收不到完成事件。
- `hub/api/devices.py:123-138` 设备密钥轮换：新密钥 `UPDATE` 立即提交；若随后的 `store.audit()` 失败（磁盘满、锁超时），异常冒泡，新密钥不会返回给用户，而旧密钥已失效——设备下次重连即被锁死，只能再轮换一次。
- `hub/runtime.py:896-899` 入队：`INSERT operations` 提交后才写审计，审计失败时出现「已入队但无审计」的操作。

**规模**：AST 扫描在事务块内找到 **355 处** 直接调用（`hub/collaboration/*` 占多数：`rooms.py` 26、`coordination.py` 25、`service.py` 24、`events.py` 21 …；`hub/runtime.py` 8、`hub/api/devices.py` 7）。这还不包括跨函数的间接调用。

**协作模块中的跨函数实例（精读补充）**——这些是嵌套调用另一个自带事务的函数，AST 扫描看不到：

| 位置 | 嵌套调用 | 后果 |
| --- | --- | --- |
| `coordination.py` `admit()`（目标工作执行） | `runtime._admit_operation()` 内部 `with store.transaction()` + `INSERT operations` | **最严重**：操作在这一刻已作为 `queued` 提交。之后的 `coordination_operations` 关联、把 `bounded_execution` 硬超时写回加密 payload、`deadline=MIN(...)` 收紧、`steps+1` 计数，任一步失败都回滚不了已提交的操作——它仍会被投递执行，但不属于任何目标、不受硬超时与步数预算约束 |
| `coordination.py` `stop_work()`（目标暂停/取消/修订/重新批准时调用） | `runtime.complete()` 自带 `transaction()` | 暂停/取消的外层事务在中途被提交，后续失败只能部分回滚 |
| `team_collaboration.py:445-458`（团队派发目标） | `q.create()`、`q.approve()` 各自开嵌套事务 | 目标与启动工作项先被提交并发出工作通知；随后 `INSERT team_goals` 失败会留下没有团队关联的孤儿活跃目标。另外 `approve()` 在团队关联写入前就按普通路径发出了一次 `work_available` 事件 |

这三处在 P1 的 `Store` 修复合入后会自动恢复原子性（嵌套变 SAVEPOINT），不需要单独改业务代码；但应补对应的失败注入测试。

**复现**（附录 A.1，已复现）：外层事务中先 `store.execute(UPDATE)` 再抛异常，另一连接读到的仍是新状态。

```
in_transaction after store.execute(): False
state seen by another connection: succeeded
outbox rows: 0
```

第四轮又找到一处靠回滚兜底的写法：`room_pending_bindings.activate()`（147-185）的注释写着「如果当前授权或桥接建立失败，SQLite 会恢复原连接」。实际顺序是先 `stop_member_work(revoke=True)`、改写 `room_members`，再建桥接和激活消费者。中间任何一次 `store.execute()` 提前提交后，后面一旦失败，成员的旧连接已经拆掉，新连接却没建成。

**修复**：让 `execute()` 在已有事务时加入而不提交，让嵌套的 `transaction()` 使用 SAVEPOINT。

```python
# hub/store.py
import itertools

class Store:
    def __init__(self, ...):
        ...
        self._savepoints = itertools.count(1)

    @contextmanager
    def transaction(self, *, immediate=True):
        """Outermost call owns BEGIN/COMMIT; nested calls use a SAVEPOINT and never commit."""
        with self.lock:
            if self.db.in_transaction:
                name = f"sp_{next(self._savepoints)}"
                self.db.execute(f"SAVEPOINT {name}")
                try:
                    yield self.db
                except BaseException:
                    self.db.execute(f"ROLLBACK TO {name}")
                    self.db.execute(f"RELEASE {name}")
                    raise
                self.db.execute(f"RELEASE {name}")
                return
            self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            self.db.commit()

    def execute(self, sql: str, args=()):
        with self.lock:
            if self.db.in_transaction:      # 加入调用方的工作单元，由最外层负责提交
                return self.db.execute(sql, args)
            with self.db:
                return self.db.execute(sql, args)
```

已验证四种场景（附录 A.1 的修复版）：外层失败全部回滚 ✅；成功正常提交 ✅；内层失败被捕获时只回滚 SAVEPOINT ✅；事务外独立调用仍自动提交 ✅。

**配套工作**

1. `with store.lock, store.db:` 依赖 Python 的「首条 DML 隐式 BEGIN」，第一条写入如果走 `store.execute()` 仍会单独提交。把这些块统一改成 `with store.transaction():`（`hub/api/devices.py`、`hub/oauth.py`、`hub/auth.py` 等）。
2. 少数地方可能 **依赖** 了提前提交（例如先落租约再做网络 I/O）。修复后跑一遍全量回归，重点看 `hub/collaboration/*` 的租约/围栏逻辑。
3. 把附录 A.5 的 AST 扫描脚本加入 CI，防止回归。

---

<a id="p2"></a>
### P2 🔴 Hub 以明文永久保存所有操作结果

**位置**：`hub/runtime.py:1330`（`encoded = json.dumps(result)`）、`hub/runtime.py:1366`（写入 `operations.result`）

请求载荷 `payload` 是加密存储、完成后清空的；但结果 `result` 是整个结果 JSON 的明文：

- `read` 的结果包含文件全文（单次最多 1 MB）；
- `search` 包含命中行；
- `exec` 包含输出尾部（最多 128 KB，另存 `output` 列）。

同时，整个 `hub/` 里没有任何 `DELETE FROM operations` 或 `DELETE FROM audit`（只有 `operation_output_tails` 会随完成删除）。

**影响**

- Hub 数据库随使用量线性增长：每天读 500 个平均 20 KB 的文件，一年约 3.6 GB，仅源码就占大头；SQLite 备份、`panel_state_archive` 归档也随之膨胀。
- 被授权读过的源码、命令输出、日志里出现的密钥，都以明文长期留存在 Hub 和它的每一份备份里。README 提到数据会「按使用流程传给 Hub」，但没有说明会无限期保存。
- `op_recent` 索引之外的历史扫描（面板活动页、用量统计）随表增长变慢。

**修复一：保留期清理**（建议默认 30 天操作 / 365 天审计，环境变量可调）

```python
# hub/runtime.py
OPERATION_RETENTION_DAYS = int(os.getenv("HUB_OPERATION_RETENTION_DAYS", "30"))
AUDIT_RETENTION_DAYS = int(os.getenv("HUB_AUDIT_RETENTION_DAYS", "365"))

def prune_history(self, *, now=None, batch=500):
    """Delete terminal operations past retention in small batches.
    coordination_operations references operations without ON DELETE CASCADE,
    so goal-linked operations stay until the goal itself is archived."""
    now = time.time() if now is None else now
    cutoff = now - OPERATION_RETENTION_DAYS * 86400
    removed = 0
    while True:
        with self.store.transaction():
            ids = [r["id"] for r in self.store.all(
                "SELECT o.id FROM operations o WHERE o.state IN ('succeeded','failed','cancelled') "
                "AND o.updated<? AND NOT EXISTS (SELECT 1 FROM coordination_operations c WHERE c.operation_id=o.id) "
                "ORDER BY o.updated LIMIT ?", (cutoff, batch))]
            if not ids:
                break
            marks = ",".join("?" * len(ids))
            self.store.db.execute(f"DELETE FROM operation_events WHERE operation_id IN ({marks})", ids)
            # operation_output_tails / mcp_tasks are ON DELETE CASCADE
            self.store.db.execute(f"DELETE FROM operations WHERE id IN ({marks})", ids)
        removed += len(ids)
    with self.store.transaction():
        self.store.db.execute("DELETE FROM audit WHERE at<?", (now - AUDIT_RETENTION_DAYS * 86400,))
    return removed
```

由现有的周期任务（或新增一个每 6 小时的后台任务）通过 `store.run(runtime.prune_history)` 调用；首次上线可能删除大量行，建议之后执行一次 `PRAGMA wal_checkpoint(TRUNCATE)`，并在维护窗口做一次 `VACUUM`。`needs_review` 状态不在删除范围内，保持人工处理。

**修复二：结果瘦身或加密**

- 最小改动：完成时只保留结果的结构化摘要（大小、SHA、行数、退出码），把正文放进带过期时间的 `operation_output_tails` 式附表，过期即删。MCP 客户端补读窗口通常是分钟到小时级，不需要永久保存正文。
- 若需完整保留：复用 `store.encrypt()` 加密 `result` 列。读取点集中在 `hub/runtime.py`、`hub/collaboration/coordination.py`、`hub/incoming_files.py`、`hub/native_file_ingress.py`（约 41 处 `result` 访问），建议先收敛成一个 `Runtime.operation_result(row)` 访问器再切换存储格式，用前缀区分新旧格式实现在线迁移。

---

<a id="p3"></a>
### P3 🔴 Agent 端状态目录无限增长

**位置**：`agent/journal.py`（全文）、`agent/filesystem.py:710-765`（`checkpoint`）

`agent/` 下没有任何 `DELETE FROM calls/backups/cancellations`，也没有删除备份或检查点文件的代码：

| 数据 | 每次产生 | 位置 |
| --- | --- | --- |
| `calls` 行 | 完整结果 JSON + 最多 128 KB 输出 | `state/agent.sqlite3` |
| 文件备份 | 每次 write / edit / move 保存旧内容，单个最多 1 MB | `state/backups/<id>` + `backups` 表 |
| 项目检查点 | 整个项目的 zip，默认上限 100 MB（可配到 4 GB） | `state/checkpoints/<id>.zip` |
| `cancellations` 行 | 每次取消 | `state/agent.sqlite3` |

对照：`agent/artifacts.py` 已经有配额（2 GiB / 1000 项）和过期回收，说明这是遗漏而非设计。家用电脑或小 VPS 上长期运行的 Agent 会被慢慢写满磁盘；`journal.history()` 也会越来越慢。

**修复**：新增 `Journal.prune()`，在 Agent 启动时和每 6 小时调用一次（放到线程里执行，见 P6）。只删除「已确认送达」且超过保留期的记录，绝不碰未 ACK 结果和执行中的调用。

```python
# agent/journal.py
DAY = 86400

def prune(self, *, now=None, call_days=30, max_calls=20000, backup_days=30,
          backups_per_path=20, checkpoint_days=14, max_checkpoints=10):
    now = time.time() if now is None else now
    removed = {"calls": 0, "backups": 0, "checkpoints": 0, "cancellations": 0}
    with self.lock, self.db:
        removed["calls"] = self.db.execute(
            "DELETE FROM calls WHERE acked=1 AND status IN ('done','interrupted') AND "
            "(at<? OR id NOT IN (SELECT id FROM calls ORDER BY at DESC LIMIT ?))",
            (now - call_days * DAY, max_calls)).rowcount
        removed["cancellations"] = self.db.execute(
            "DELETE FROM cancellations WHERE at<? AND id NOT IN "
            "(SELECT id FROM calls WHERE status IN ('accepted','running','retryable'))",
            (now - 7 * DAY,)).rowcount
        stale = [r["id"] for r in self.db.execute(
            "SELECT id FROM (SELECT id, at, ROW_NUMBER() OVER "
            "(PARTITION BY root, path ORDER BY at DESC) AS n FROM backups) WHERE n>? OR at<?",
            (backups_per_path, now - backup_days * DAY))]
        self.db.executemany("DELETE FROM backups WHERE id=?", [(i,) for i in stale])
    for identifier in stale:            # 先删行再删文件：崩溃只会留下孤儿文件，不会留下悬空记录
        (self.backups / identifier).unlink(missing_ok=True)
    removed["backups"] = len(stale)
    archives = sorted(self.checkpoints.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for index, archive in enumerate(archives):
        if index >= max_checkpoints or archive.stat().st_mtime < now - checkpoint_days * DAY:
            archive.unlink(missing_ok=True)
            removed["checkpoints"] += 1
    return removed
```

已用真实 `Journal` 验证（附录 A.4）：5 条 40 天前已 ACK 的调用被删；未 ACK 与执行中的调用保留；同一文件 25 份备份保留最新 20 份；12 个检查点保留最新 10 个。

保留参数建议放进 Agent 配置（`journal_retention`），并在面板设备页显示 state 目录大小。检查点被删后，面板中对应的「恢复」入口要给出明确的「已过期」提示。

---

<a id="p4"></a>
### P4 🟠 Hub→Agent 安全通道可被重放

**位置**：`shared/crypto.py:43-47`、`agent/runner.py:290-299`、`hub/runtime.py:1550-1556`

`SecureChannel` 的会话密钥为 `HKDF(secret, salt=challenge)`，challenge 只由 Hub 生成并以明文发送；Agent 只检查长度（≥32），自己不贡献任何随机数。这对 Hub 是安全的（Hub 每次都发新 challenge），但对 Agent 不是：

在明文 `ws://` 部署下（这正是该通道存在的意义），旁路记录过一次会话的攻击者，之后可以冒充 Hub：把旧 challenge 发给重连的 Agent，再按原序重放录下的密文。序号从 1 开始，与新会话完全一致，Agent 全部接受。

- `call` 消息有 journal 的操作 ID 去重，重放只会返回旧结果；
- 但 `native_request`（向 Codex / Claude Code 会话发送消息）、`cancel`、`cancel_pending`、`scheduler_config`、`computer_approval_decision`、`artifact` 请求都没有按 ID 去重，**会被重新执行**。

**复现**（附录 A.2，已复现）：

```
agent accepted: {'type': 'ready'}
agent accepted: {'type': 'native_request', 'request_id': 'r1', 'action': 'send', 'text': 'rm -rf build && ./deploy.sh'}
agent accepted: {'type': 'cancel_pending', 'id': 'op-123'}
```

**修复**：Agent 在 `hello` 中携带新随机数，Hub 在第一条加密回复 `ready` 中回显；Agent 校验不一致即断开。因为 `ready` 在同一密钥和序号链上，攻击者无法伪造带新随机数的 `ready`，后续重放自然失效。为防降级（攻击者重放升级前的旧会话），Agent 一旦见过支持绑定的 Hub，就把这一事实持久化，之后强制要求。

```python
# agent/runner.py — connect_once()
agent_nonce = token(32)
await socket.send(channel.pack({"type": "hello", ...,
                                "channel_binding": {"version": 1, "agent_nonce": agent_nonce}}))
reply = channel.unpack(await asyncio.wait_for(socket.recv(), 10))
if reply.get("type") != "ready":
    raise ValueError("面板未确认 Agent 身份")
echoed = (reply.get("channel_binding") or {}).get("agent_nonce")
pinned = self.journal.meta("hub_channel_binding") == "1"
if echoed is None:
    if pinned:
        raise ValueError("面板未回显通道绑定，拒绝连接（可能是重放或降级）")
elif not hmac.compare_digest(str(echoed), agent_nonce):
    raise ValueError("面板会话不新鲜，拒绝连接")
elif not pinned:
    self.journal.set_meta("hub_channel_binding", "1")
```

```python
# hub/runtime.py — agent_socket()，组装 ready 回复处
binding = hello.get("channel_binding")
ready = {"type": "ready", ...}
if (isinstance(binding, dict) and binding.get("version") == 1
        and isinstance(binding.get("agent_nonce"), str) and 32 <= len(binding["agent_nonce"]) <= 128):
    ready["channel_binding"] = {"agent_nonce": binding["agent_nonce"]}
```

`Journal` 需要两个小方法 `meta(key)` / `set_meta(key, value)`，读写已有的 `meta` 表即可。已用真实 `SecureChannel` 验证（附录 A.2 修复版）：正常握手通过，重放的 `ready` 被拒绝。

更彻底的做法是把 Agent 随机数也纳入密钥派生（`salt = challenge ‖ agent_nonce`），但那需要先明文交换一次随机数、改动握手顺序；上面的回显方案改动更小，安全效果等价。无论哪种，都应在文档中继续强调：公网部署必须使用 HTTPS/WSS。

---

<a id="p5"></a>
### P5 🟠 配置备份含设备密钥，可被文件工具读取

**位置**：`agent/runtime_config.py:103`、`agent/filesystem.py:125`

面板每次修改 Agent 配置（运行时、授权目录），`_replace_config()` 都会在 `config.json` 旁边写一份 `config.json.runtime-backup-<uuid>`，内容是完整旧配置（包括设备 `secret`），且永不删除。

文件工具对配置的保护是精确比较：

```python
if resolved == self.config_path or within(resolved, self.journal.directory):
    raise DevError("PROTECTED_PATH", ...)
```

默认目录 `~/.codepier-agent` 因名字在 `IGNORED_DIRS` 里而被整体保护，所以默认安装不受影响。但只要管理员用 `--config` 把配置放在某个授权目录之内（例如 `/srv/projects/ops/codepier/config.json`），任何拥有该项目 `read` 权限的 MCP 客户端都能读出备份文件，拿到设备密钥后即可冒充这台 Agent 连接 Hub。`config.json` 自己被拦了，备份却没有——这说明威胁模型本来就包含这种情况。

另外，精确比较在大小写不敏感的卷上（macOS APFS 默认、Windows NTFS）可以用 `CONFIG.JSON` 之类的变体绕过。

**复现**（附录 A.3，已复现）：

```
config.json: blocked -> PROTECTED_PATH
backup: READABLE, device secret leaked
```

**修复**：

```python
# agent/filesystem.py
def agent_private(self, resolved: Path) -> bool:
    cfg = self.config_path
    key = lambda p: os.path.normcase(str(p)).casefold()   # APFS / NTFS 大小写不敏感
    if key(resolved) == key(cfg) or within(resolved, self.journal.directory):
        return True
    # runtime_config.py 写在 config.json 旁边的 <name>.runtime-backup-* / <name>.runtime-*
    return (key(resolved.parent) == key(cfg.parent)
            and key(resolved.name).startswith(key(cfg.name) + "."))

def path(self, root: Path, value: str, allow_dot: bool = True) -> Path:
    ...                                          # 现有的逐段符号链接检查保持不变
    resolved = current.resolve(strict=False)
    if not within(resolved, root):
        raise DevError("OUTSIDE_PROJECT", "路径超出项目范围", 403)
    if self.agent_private(resolved):
        raise DevError("PROTECTED_PATH", "不能访问 Agent 配置、配置备份或状态目录", 403)
    return current
```

同时把备份移到状态目录并限制份数，从根上消除问题：

```python
# agent/runtime_config.py — _replace_config()
backups = Path(config_state_dir(path)) / "config-backups"     # state_dir 已被 path() 整体保护
backups.mkdir(mode=0o700, exist_ok=True)
backup = backups / f"{path.name}.{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
...
for old in sorted(backups.glob(path.name + ".*"))[:-10]:      # 只保留最近 10 份
    old.unlink(missing_ok=True)
```

已验证（附录 A.3 修复版）：原名、备份、大小写变体全部返回 `PROTECTED_PATH`，同目录普通文件仍可读。

`root()` 第 108 行的 `A or B and C` 按优先级等价于「根目录恰为配置目录时拒绝」。它是冗余写法而不是漏洞：改成「配置目录内一律拒绝」会误伤 `--config ~/config.json` 这类布局。建议直接写成 `if within(actual, self.journal.directory) or actual == self.config_path.parent:`，真正的防线放在上面的逐文件检查。

---

<a id="p6"></a>
### P6 🟠 Agent 在事件循环线程里同步写 SQLite

**位置**：`agent/runner.py:1205-1214`（`snapshot()`）以及 `journal.start/finish/mark_running/is_cancelled/status` 的所有调用点

Agent 只有一个 asyncio 事件循环，同时承担 WebSocket 心跳、所有并发任务和输出转发。而：

- exec 运行期间每 250ms 调用一次 `journal.update_output()`，每次把完整的 128 KB 输出尾部写进 SQLite 并提交；
- journal 使用 WAL + 默认 `synchronous=FULL`，每次提交都 fsync；
- 这些调用都直接在事件循环线程上执行。

普通 SSD 上每次几毫秒，问题不大；状态目录在 HDD、外置盘、SMB/NFS 或负载很高时，单次可达数十到数百毫秒，所有并发任务、心跳和取消处理都会被卡住。另外 `append_output()` 每收到 64 KB 就拼接并切片一个 128 KB 字符串。

**修复**

```python
# agent/journal.py — __init__
self.db.execute("PRAGMA synchronous=NORMAL")   # WAL 下不会损坏数据库，掉电最多丢最后一次提交

# agent/runner.py — 输出快照：内存转发保持 250ms，落盘降到 2s 且移出事件循环
JOURNAL_SNAPSHOT_SECONDS = 2.0

def snapshot(force=False):
    nonlocal seq, last_snapshot, last_snapshot_output, last_persist
    ...
    self.pending_output[id] = {...}            # 实时推送不变
    self.output_wake.set()
    if force or time.monotonic() - last_persist >= JOURNAL_SNAPSHOT_SECONDS:
        last_persist = time.monotonic()
        persist = asyncio.get_running_loop().run_in_executor(
            self.journal_executor, self.journal.update_output, id, decoded_output, seq)
        persist.add_done_callback(self._log_persist_failure)
```

`journal_executor` 用一个单线程 `ThreadPoolExecutor(max_workers=1)`，既保证写入顺序，又让 `journal.lock` 的争用离开事件循环。`start`/`finish` 等低频写可以先保持同步，等 `update_output` 迁走后再看是否还需要。

输出缓冲建议换成 `collections.deque` 按块存放，只在快照时 `"".join()`，避免每块都复制 128 KB。

---

<a id="p7"></a>
### P7 🟡 登录成功会清空该 IP 的全部失败计数

**位置**：`hub/auth.py:151`

```python
self.store.db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
```

任何账号登录成功都会清掉这个 IP 的所有失败记录。持有一个普通账号的人可以「猜 7 次目标账号 → 用自己账号登录一次 → 再猜 7 次」，让每 IP 5 分钟 8 次的限制失效。每账号 8 次的限制仍然有效，所以单个账号的爆破速度上限不变，但攻击者可以在同一个 IP 上同时爆破很多账号。

**修复**：只抵消属于本账号的失败次数。

```python
# hub/auth.py — _login_session()
own_failures = self.store.db.execute(
    "SELECT count(*) FROM login_account_attempts WHERE username_hash=?",
    (digest(username),)).fetchone()[0]
self.store.db.execute(
    "DELETE FROM login_attempts WHERE rowid IN "
    "(SELECT rowid FROM login_attempts WHERE ip=? ORDER BY at DESC LIMIT ?)",
    (ip, own_failures))
self.store.db.execute("DELETE FROM login_account_attempts WHERE username_hash=?", (digest(username),))
```

（更严谨的做法是给 `login_attempts` 增加 `username_hash` 列，精确删除；上面的写法不需要改表结构。）

---

<a id="p8"></a>
### P8 🟡 scrypt 参数偏低，且无升级路径

**位置**：`shared/crypto.py:22-34`

当前为 N=2¹⁴、r=8、p=1（约 16 MiB，实测 46 ms），是 2009 年论文中的交互式下限。建议提到 N=2¹⁵（约 32 MiB，实测约 100 ms），并在登录成功时透明重哈希。

注意：OpenSSL 默认 `maxmem` 是 32 MiB，N=2¹⁵ 不传 `maxmem` 会直接抛 `memory limit exceeded`（已实测）。

```python
# shared/crypto.py
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024

def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                             p=SCRYPT_P, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"

def password_verify(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt, expected = encoded.split("$")
        if int(n) > 1 << 17 or int(r) > 16 or int(p) > 4:      # 拒绝数据库中被篡改的超大参数
            return False
        derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n),
                                 r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM * 4)
        return hmac.compare_digest(derived.hex(), expected)
    except (ValueError, TypeError):
        return False

def password_needs_rehash(encoded: str) -> bool:
    try:
        _, n, r, p, *_ = encoded.split("$")
        return (int(n), int(r), int(p)) < (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except ValueError:
        return True
```

在 `Auth._login_session()` 校验通过后，若 `password_needs_rehash(user["password_hash"])`，用 `password_hash(password)` 更新 `users.password_hash`（需要把明文密码传进来，或在 `login()` 里完成）。`Auth.dummy` 也要用新参数生成，保持未知用户的耗时一致。登录并发上限是 4，内存峰值约 128 MiB，可以接受。

---

<a id="p9"></a>
### P9 🟡 k3s 部署信任整个 Pod 网段的转发头

**位置**：`deploy/k3s/deployment.yaml:48`

```yaml
FORWARDED_ALLOW_IPS: 10.42.0.0/16
```

uvicorn 会信任来自该网段任意地址的 `X-Forwarded-For`。集群内任何 Pod（包括与 CodePier 无关的工作负载）都能伪造来源 IP，绕过登录与 OAuth 的按 IP 限流，并污染审计中的 IP。

**修复**：改为 Ingress Controller 所在 Pod 的固定地址，或用 NetworkPolicy 只允许 Ingress 命名空间访问 Hub 的 8765 端口：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: codepier-hub-ingress-only }
spec:
  podSelector: { matchLabels: { app: codepier-hub } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
          podSelector: { matchLabels: { app.kubernetes.io/name: traefik } }
      ports: [{ port: 8765 }]
```

`compose.yml` 与 `deploy/compose.https.yml` 的配置是正确的，`docs/START-HERE.md` 也已提示不要改成 `*`。

---

<a id="p10"></a>
### P10 🟡 Hub async 函数内的同步数据库访问

Hub 普遍通过 `store.run()` 把数据库操作放到工作线程，扫描只发现 3 处遗漏：

| 位置 | 调用 |
| --- | --- |
| `hub/agent_install.py:340` `lifecycle_action` | `store.one('SELECT name FROM devices WHERE id=?', ...)` |
| `hub/artifacts.py:197` `download` | `runtime.store.audit(owner.actor, 'artifact.download', ...)` |
| `hub/panel_update.py:165` `submit` | `runtime.store.audit(principal.actor, 'panel_update.requested', ...)` |

改为 `await store.run(...)` 即可，例如：

```python
await runtime.store.run(runtime.store.audit, owner.actor, 'artifact.download', identifier, detail=...)
```

`artifacts.download` 是下载路径，审计写入在锁竞争时会阻塞整个事件循环，优先处理。

---

<a id="p11"></a>
### P11 ⚪ 死代码与冗余

| 位置 | 问题 | 处理 |
| --- | --- | --- |
| `hub/runtime.py:1204` | `grant = self.store.one('SELECT * FROM grants ...')` 结果未使用；每次持久投递多一次查询 | 删除 |
| `hub/auth.py:146` | `_login_session` 里的 `secret, csrf` 未使用（`new_session()` 自己生成） | 改为 `now = time.time()` |
| `hub/gateway/registry.py:255` | `grant = owned_grant(...)` 变量未使用（调用本身是授权检查，需保留） | 去掉赋值 |
| `agent/filesystem.py:25` | `IGNORED_DIRS` 中 `".remote-dev"` 重复 | 删除重复项 |
| `agent/__main__.py:52` | 函数内重复 `import default_base` | 删除 |
| `hub/api/{accounts,activity,devices,projects,settings}.py` | 解包出的 `config/maintenance/public_url/BASE` 未使用 | 精简解包 |
| `hub/collaboration/room_runtime.py:213,749`、`team_collaboration.py:277,450` | `scopes`、`sent`、`goals`、`approved` 赋值后未使用 | 逐个确认：若是遗漏的检查要补上，否则删除 |
| `shared/mcp_presentation.py:572`、`agent/macos_app.py:89` | `execution`、`binary` 未使用 | 同上 |
| `Dockerfile` | 应用代码 `--chown=codepier`，运行用户可改写自己的代码 | 代码目录改为 `root:root`，只有 `/app/data` 归 `codepier`（compose 已设只读根文件系统，k3s 也建议设 `readOnlyRootFilesystem: true`） |

`team_collaboration.py:450` 的 `approved` 和 `room_runtime.py:213` 的 `scopes` 名字看起来像权限判断的中间结果，建议优先人工确认不是漏写了检查。

---

<a id="p12"></a>
### P12 🔴 渠道轮询：一条坏消息卡死整个 bot

**位置**：`hub/collaboration/telegram.py:424-538`（`commit_updates`）、`telegram.py:727-748`（`poll_failed`）；`hub/collaboration/weixin.py:370-392`、`weixin.py:533-553`

`commit_updates()` 把一批（最多 100 条）更新放在一个事务里逐条 `ingest()`。只要 **任意一条** 抛出 `DevError`，整批回滚、`offset` 不前进，下一轮 `getUpdates` 拿回同一批，再次失败。会让单条更新抛错的情况包括：

- `TELEGRAM_DOT_QUOTA`（429）：目标 dot 未处理消息 ≥500，比如 dot 离线一段时间；
- `TELEGRAM_REPLY_SCOPE`（403）：回复的原消息映射到别的房间/话题；
- `NOT_FOUND`（404）：用户在 Telegram 里「回复」一条旧消息，而所有者对那条消息涉及的某个项目已失去访问权（项目被移除映射或权限收回）；
- `valid()` 抛出的绑定/授权错误。

`poll_failed()` 的处理让后果更重：

- 401/403/409/422 → **整个 bot 账号**转为 `attention`，同一个 bot 上所有房间的绑定一起停收；所有者恢复后，毒消息仍在队首，立即再次触发；
- 其它状态（404、429 等）→ 账号保持 active，每 5 秒用同一个 offset 重试，永远失败。Telegram 只保留未确认更新 24 小时，之后整批——包括其他人的正常消息——被丢弃。

微信是同样的结构（注释明确写着「整批保留原游标」）。它对未知分项类型处理得当（渲染为占位文字），但格式异常（分项不是对象、超过 32 项）会抛 422 使账号进入 `attention`；`WEIXIN_INBOX_QUOTA` 会指数退避到 1 小时。微信是「一个机器人一个话题」，影响面比 Telegram 小。

第四轮补充的两个触发条件（原生房间微信入口 `room_weixin_native.py:469-499`，旧入口 `weixin.py` 同样如此）：

- 已配对联系人发来一段超过 12000 字节的文字（约 4000 个汉字），或几段文字合计超过 24000 字节，`text()` 会抛 `WEIXIN_FIELD_INVALID`（422）。整批回滚后，账号被标为 `attention`。
- 应答里 `to_user_id` 与机器人不一致，抛 409。

超长文本应截断并标注「已截断」，而不是拒收整批消息。

**修复**：把错误分成「批级」和「消息级」。批级错误（世代失效、绑定变化、批次超限、update_id 非法/冲突、每日配额）仍然整批放弃；消息级错误只回滚这一条（SAVEPOINT），记录原因后继续，并推进 offset。背压类错误（dot 积压）不丢正文，标为 `deferred`，积压消化后从已加密保存的 `telegram_inbox.body` 重放。

```python
# hub/collaboration/telegram.py — commit_updates() 循环体（原有去重、每日配额与 INSERT telegram_inbox 保持不变）
DEFERRABLE = {'TELEGRAM_DOT_QUOTA'}                    # 背压：保存原文，稍后重放
BATCH_SCOPED = {'TELEGRAM_POLL_FENCE_LOST', 'TELEGRAM_BINDING_CHANGED', 'TELEGRAM_BATCH_LIMIT',
                'TELEGRAM_UPDATE_INVALID', 'TELEGRAM_UPDATE_CONFLICT', 'TELEGRAM_INBOX_QUOTA'}

                self.store.execute("INSERT INTO telegram_inbox ...")      # 原样保留：先落原文
                self.store.execute("SAVEPOINT telegram_update")
                try:
                    mid, reason = self.ingest(account, update, claim["bindings"])
                except DevError as exc:
                    self.store.execute("ROLLBACK TO telegram_update")
                    self.store.execute("RELEASE telegram_update")
                    if exc.code in BATCH_SCOPED:
                        raise
                    mid = None
                    reason = ("deferred:" if exc.code in DEFERRABLE else "rejected:") + exc.code
                else:
                    self.store.execute("RELEASE telegram_update")
                state = "committed" if mid else "deferred" if reason.startswith("deferred:") else "quarantined"
                self.store.execute(
                    "UPDATE telegram_inbox SET state=?,message_id=?,reason_code=? WHERE account_id=? AND update_id=?",
                    (state, mid, reason, account["id"], uid))
                if mid:
                    ids.append(mid)
                offset = max(offset, uid + 1)                          # 消息级错误也前进，不再卡住队首
```

```python
# hub/collaboration/telegram.py — 新增：积压消化后重放 deferred 原文（由 tick() 在轮询前调用）
def replay_deferred(self, account_id, limit=20):
    with self.store.transaction():
        account = self.store.one("SELECT * FROM telegram_accounts WHERE id=?", (account_id,))
        eligible = {b["id"]: b["version"] for b in self.store.all(
            "SELECT id,version FROM telegram_bindings WHERE account_id=? AND state IN ('active','pairing')", (account_id,))}
        for row in self.store.all("""SELECT * FROM telegram_inbox WHERE account_id=? AND state='deferred'
                ORDER BY update_id LIMIT ?""", (account_id, limit)):
            update = json.loads(self.store.decrypt(row["body"]))
            self.store.execute("SAVEPOINT telegram_replay")
            try:
                mid, reason = self.ingest(account, update, eligible)
            except DevError as exc:
                self.store.execute("ROLLBACK TO telegram_replay"); self.store.execute("RELEASE telegram_replay")
                if exc.code == "TELEGRAM_DOT_QUOTA":
                    break                                          # 仍在积压：按顺序等待，不跳过
                mid, reason = None, "rejected:" + exc.code
            else:
                self.store.execute("RELEASE telegram_replay")
            self.store.execute("UPDATE telegram_inbox SET state=?,message_id=?,reason_code=? WHERE account_id=? AND update_id=?",
                ("committed" if mid else "quarantined", mid, reason, account_id, row["update_id"]))
```

微信同理：在 `weixin.commit_updates()` 的 `for message in messages` 内对 `ingest_one()` 加 SAVEPOINT；批级集合为 `WEIXIN_POLL_FENCE_LOST`、`WEIXIN_BINDING_CHANGED`、`WEIXIN_BATCH_LIMIT`、`WEIXIN_DAILY_QUOTA`，`WEIXIN_INBOX_QUOTA` 作为可延后，其余消息级错误写入 `weixin_inbound(disposition='rejected:<code>')` 后继续。

面板渠道页应展示 `quarantined` / `deferred` 计数与原因码，让所有者知道有消息被隔离，而不是看到整个渠道「无响应」。

---

<a id="p13"></a>
### P13 🔴 本地检出总是落后 GitHub：隔离目录的生命周期缺口

**这是 CodePier 自身工作流的问题，不是使用者操作失误。** 你通过 CodePier 让 AI 改代码时，标准做法是在隔离目录（worktree）里开分支、推送、合并；而 CodePier 只负责「创建」和「手动删除」隔离目录，中间的基线选择、合并跟踪、收尾和回写主检出都没有覆盖。

**在你的 Mac 上实测（2026-11-04 20:55）**

| 项目 | 现状 |
| --- | --- |
| 主检出 `~/Downloads/remote-dev-mcp` | `main` 停在 `3f004f5`（10-25），相对 `origin/main` **领先 0 / 落后 109**；117 处未提交改动，内容已以其它提交进入 `main` |
| `git stash` | 4 条，名字都是「同步上游前保存工作区」，说明这种手工对齐反复发生 |
| worktree 总数 | 22 个：19 个 `~/Downloads/CodePier-Worktrees/<root 哈希>/<操作 ID>`、1 个 Codex、8 个分离 HEAD |
| 已合并却未清理 | 13 个分支 worktree 已完全合并进 `origin/main` 仍然保留 |
| 合并后仍有改动 | `release/v1.32.0-collaboration-center` 104 处、`release/v1.30.5-pairing-options` 9 处——**可能是遗漏未提交的工作**，删除前必须人工审阅 |
| 项目内临时工作目录 | 含历史工作树与构建产物，具体占用和本机路径已脱敏 |

**根因（代码）**

1. **默认基线是陈旧的本地 HEAD**：`shared/integration_contracts.py:109` 中 `base_ref` 默认 `'HEAD'`，`agent/workspaces.py:40` 在源检出里直接 `rev-parse`，创建前不 `fetch`。主检出一旦落后，之后按默认参数创建的每个隔离目录都从旧代码起步，合并时冲突或重复劳动。
2. **只有分离 HEAD，不跟踪后续**：`workspaces.py:62` 用 `worktree add --detach`。之后 AI 在里面自建分支、推送、开 PR，CodePier 对分支、上游、合并状态一无所知，列表里也看不出哪个已经合并。
3. **没有收尾**：只能按 ID 手动删除，脏目录拒绝删除（这是对的），但没有任何「已合并且干净 → 可清理」的提示或批量操作；配额 64 个，迟早会撞上。
4. **从不回写主检出**：源目录的未提交改动不会被带进隔离目录（`workspaces.py:68` 的提示），合并后也没有环节把主检出快进。于是主检出同时保留着「旧基线 + 已经在上游的重复改动」。
5. **打开项目时看不到新鲜度**：`project_query(open)` 和 `hub/workspace_status.py` 都不报告领先/落后/脏状态。AI 打开项目时完全不知道自己读的是旧代码——本次审计第一轮就是这样先读到了旧版本，靠手工比对哈希才发现。实际上这台机器上的 `origin/main` 引用是新的（worktree 的推送/拉取会更新共享引用），**不联网**就能算出「落后 109」。

**修复一：默认基线改为「先 fetch，再取 `origin/HEAD`」**（已用临时仓库验证：旧默认得到落后 3 个提交的基线，新逻辑得到最新提交并报告 `local_head_behind: 3`）

```python
# shared/integration_contracts.py
base_ref: str = Field(default='', max_length=200)          # '' = auto
fetch: bool = True
branch: str = Field(default='', max_length=120)           # 可选：创建命名分支，便于跟踪

# agent/workspaces.py
async def resolve_base(self, identifier, project, root, base_ref, fetch):
    report = {'requested': base_ref or 'auto', 'fetched': False, 'fetch_error': None}
    if fetch:
        try:
            await self.git(identifier, project, root, ['fetch', '--prune', '--quiet', 'origin'])
            report['fetched'] = True
        except DevError as exc:                         # 离线时退回本地引用，但如实报告
            report['fetch_error'] = exc.code
    for ref in ([base_ref] if base_ref else ['refs/remotes/origin/HEAD', '@{upstream}', 'HEAD']):
        try:
            commit = await self.git(identifier, project, root,
                                    ['rev-parse', '--verify', '--quiet', '--end-of-options', ref + '^{commit}'])
        except DevError:
            continue
        behind = await self.git(identifier, project, root, ['rev-list', '--count', 'HEAD..' + commit])
        return commit, {**report, 'resolved': ref, 'source_head_behind': int(behind or 0)}
    raise DevError('GIT_BASE_REQUIRED', '找不到可用的基线提交', 409)

# create() 中替换原来的 rev-parse，并在 worktree add 时可选建分支：
commit, base = await self.resolve_base(identifier, project, root, args['base_ref'], args['fetch'])
add = ['worktree', 'add'] + (['-b', args['branch']] if args['branch'] else ['--detach']) + [str(target), commit]
```

`git fetch` 需要 `core.hooksPath` 已置空（现有 `git()` 包装已做）并有超时；没有远端的仓库会自然落到 `HEAD`。返回值里带上 `base`，让调用方看见「基线来自哪里、源检出落后多少」。

**修复二：项目打开与工作区状态报告新鲜度**（纯本地，不联网）

```python
# agent 侧，供 project_query(open) / workspace_status 调用
async def freshness(self, identifier, project, root):
    out = await self.git(identifier, project, root, ['status', '--porcelain=v2', '--branch', '--untracked-files=normal'])
    info = {'branch': None, 'upstream': None, 'ahead': 0, 'behind': 0, 'dirty': 0}
    for line in out.splitlines():
        if line.startswith('# branch.head '):
            info['branch'] = line.split(' ', 2)[2]
        elif line.startswith('# branch.upstream '):
            info['upstream'] = line.split(' ', 2)[2]
        elif line.startswith('# branch.ab '):
            ahead, behind = line.split()[2:4]
            info['ahead'], info['behind'] = int(ahead), -int(behind)
        elif line and not line.startswith('#'):
            info['dirty'] += 1
    fetch_head = Path(await self.git(identifier, project, root, ['rev-parse', '--git-path', 'FETCH_HEAD']))
    fetch_head = fetch_head if fetch_head.is_absolute() else root / fetch_head
    info['last_fetch_age_seconds'] = int(time.time() - fetch_head.stat().st_mtime) if fetch_head.exists() else None
    if info['behind']:
        info['warning'] = f"源检出落后 {info['upstream']} {info['behind']} 个提交；读取到的可能是旧代码"
    return info
```

在 `CORE_INSTRUCTIONS` 里加一句：打开项目时若 `git.behind > 0`，先告知用户，并优先在新的隔离目录（自动基线）中工作。

**修复三：合并后收尾**——新增 `workspace_prune`（默认只预览）

```python
async def prune(self, identifier, project, args):
    """Remove ready, clean, merged worktrees older than min_age_days. Dry run unless apply=True."""
    parent = self.parent_project(project)
    root, _ = self.agent.engine.root(parent, True)
    into = args.get('into') or 'refs/remotes/origin/HEAD'
    plan = []
    for record in self.records.list('workspace', parent, 2000):
        if record['state'] != 'ready' or time.time() - record['created'] < args.get('min_age_days', 3) * 86400:
            continue
        resolved = self.resolve(parent, record['workspace_id'])
        path = Path(resolved['root'])
        dirty = await self.git(identifier, resolved, path, ['status', '--porcelain=v1', '--untracked-files=all'])
        head = await self.git(identifier, resolved, path, ['rev-parse', 'HEAD'])
        check = await self.agent.run_process(identifier, ['git', 'merge-base', '--is-ancestor', head, into], root, 30,
                                             execution_project=parent)
        merged = check['exit_code'] == 0
        plan.append({'workspace_id': record['workspace_id'], 'label': record['label'], 'merged': merged,
                     'dirty': bool(dirty), 'action': 'remove' if merged and not dirty else 'keep'})
    if not args.get('apply'):
        return {'plan': plan, 'applied': False}
    removed = []
    for item in plan:
        if item['action'] == 'remove':
            await self.remove(identifier, project, {'target_workspace_id': item['workspace_id'],
                                                    'confirm': item['workspace_id']})
            removed.append(item['workspace_id'])
    return {'plan': plan, 'applied': True, 'removed': removed}
```

`merge-base --is-ancestor` 只识别普通合并与快进；若以后改用 squash 合并，应改用 `git cherry` 或 GitHub API 判断 PR 状态。脏目录永远只报告、不删除。

**修复四：可选的「同步源检出」动作**——仅当源检出干净且位于默认分支时执行 `fetch` + `merge --ff-only`；否则返回差异摘要，不做任何改写。

**修复五：`.work` 移出项目根**——验收/调试产物默认写到 Agent 状态目录或 `~/Library/Caches/CodePier/<项目>`，项目内只留一个指向它的说明文件；同时把 `.work` 加入搜索排除（见 P14）。

**现在如何安全地同步这台 Mac**（全部可撤销，先审阅再删除）

```bash
cd ~/Downloads/remote-dev-mcp
git fetch --prune origin

# 1. 主检出：先完整收起 117 处改动，再快进
git stash push -u -m "pre-sync-$(date +%Y%m%d-%H%M)"
git merge --ff-only origin/main
# 判断收起的改动是否已被上游包含：反向检查成功 = 已包含，可以不管
git stash show -p --include-untracked stash@{0} > /tmp/pre-sync.patch
git apply --check -R /tmp/pre-sync.patch && echo "已全部包含在新 main 中"

# 2. 列出已合并且干净的 worktree，逐个用 CodePier 的 workspace remove（或 git worktree remove）
git worktree list --porcelain

# 3. 合并后仍有改动的两个目录，先看清楚再决定
git -C <release/v1.32.0-collaboration-center 的目录> status --short | head -50
git -C <release/v1.32.0-collaboration-center 的目录> diff --stat

# 4. .work 里的嵌套 worktree 先正式移除，再把 .work 挪到项目外
git worktree remove <已核实且获授权的工作树路径>
```

---

<a id="p14"></a>
### P14 🟠 全项目搜索对存在的代码返回 0 条

**位置**：`agent/filesystem.py:319-371`（`walk`）、`agent/filesystem.py:408-466`（`search`）、`agent/filesystem.py:25`（`IGNORED_DIRS`）

`search` 的预算是 2 万个文件 / 10 万个目录项 / 8 秒。`walk()` 目录优先、按名字排序，于是以点开头的目录最先被扫；`IGNORED_DIRS` 只有 `.venv`，不含 `.venv-compat`、`.venv-release` 之类，也不含 `.work`；并且完全不读 `.gitignore`。在你的项目里，预算在进入 `agent/`、`hub/` 之前就耗尽了。

**实测**（通过 CodePier 自己的 `project_query(search)`）：

| 搜索 | 结果 |
| --- | --- |
| `path="."`，查 `def transaction(self, *, immediate` | **0 条命中**，`truncated: true`，扫描 3748 个文件即停 |
| `path="hub"`，同一查询 | 立即命中 `hub/store.py:189` |

`truncated: true` 很容易被模型忽略，于是得出「代码里没有这个函数」的错误结论，接着重复实现或做出错误修改。本地复现（在仓库副本中放入 6 万个被忽略文件）：现有遍历扫满 2 万文件、耗时 6.0 秒、0 命中；改用 `git ls-files` 后候选 1225 个文件、0.1 秒命中 `hub/store.py`。

**修复**：Git 仓库优先用 `git ls-files --cached --others --exclude-standard` 枚举候选（自动遵守 `.gitignore`），失败再回退到遍历；遍历时也排除 `.venv*` 前缀与 `.work`；结果里注明扫描范围与耗尽预算的目录，让模型知道该缩小路径。

```python
# agent/filesystem.py
def git_candidates(root: Path, start_rel: str, limit=20000, timeout=5):
    """Tracked + untracked-but-not-ignored files under start_rel; None when git cannot answer."""
    if not (root / '.git').exists():
        return None
    try:
        out = subprocess.run(['git', '-c', 'core.quotepath=off', '-c', 'core.fsmonitor=false', 'ls-files', '-z',
                              '--cached', '--others', '--exclude-standard', '--', start_rel or '.'],
                             cwd=root, capture_output=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = [n for n in out.decode('utf-8', 'surrogateescape').split('\0') if n and not protected(n)]
    return names[:limit], len(names) > limit

# search() 中：
candidates = git_candidates(root, start.relative_to(root).as_posix()) if start.is_dir() else None
if candidates is not None:
    names, capped = candidates
    entries = ((root / n, n, (root / n).lstat()) for n in names)    # 后续仍逐个走 self.path() 与 read_bytes() 校验
    scope = 'git_index'
else:
    entries = self.walk(root, start, deadline=budget + 8, exclude=lambda rel: PurePosixPath(rel).name.startswith(('.venv', '.work')))
    scope = 'filesystem_walk'
# 返回值增加 'scan_scope': scope，截断时附 'hint': '结果被截断；请缩小 path 或 file_glob，0 条命中不代表不存在'
```

`tree` 与 `checkpoint` 也应复用同一排除规则，避免把 `.work`、虚拟环境打进检查点。

---

<a id="p15"></a>
### P15 🟠 老部署的 OAuth 白名单缺 `claude.ai`，Claude 无法连接

**Claude 的回调地址**（[官方文档](https://claude.com/docs/connectors/building/authentication#callback-urls)）：

- claude.ai 网页、桌面端、移动端、Cowork 共用固定回调：`https://claude.ai/api/mcp/auth_callback`
- Claude Code 使用端口不固定的回环地址：`http://localhost:<port>/callback` 与 `http://127.0.0.1:<port>/callback`

CodePier 的 `validate_redirect()` 对回环地址已允许任意端口，Claude Code 不受影响；CodePier 没有声明 CIMD，Claude 会回退到 DCR，这也没问题。问题只在 `claude.ai` 这个主机名。

**根因链**

1. `55fb590`（09-20）把 `OAUTH_REDIRECT_HOSTS=chatgpt.com,chat.openai.com,platform.openai.com,localhost,127.0.0.1,::1` **显式写进** `.env.example`。
2. `deploy/install-hub.sh:69` 只在 `.env` 不存在时复制 `.env.example`。于是 09-20 之后安装的每个部署，`.env` 里都有这行不含 `claude.ai` 的显式值。
3. `c7e4207`（11-04 12:14）只修改了**默认值**（`hub/oauth.py`、`compose.yml`、`.env.example`、k3s 清单）。`.env` 里的显式值优先于 compose 默认值，所以对老部署无效。
4. 面板更新器 `scripts/panel_update_runtime.py:502` 的 `committed_env()` 只改镜像、数据卷、`COMPOSE_FILE`、`CODEPIER_HUB_SOURCE`，不迁移这行；`git pull` 更不碰 `.env`。
5. Claude 做 DCR 时 `POST /oauth/register` 带上 `https://claude.ai/api/mcp/auth_callback`，被 `validate_redirect()` 拒绝（`INVALID_REDIRECT`），Claude 侧只显示授权失败，看不出原因。

**受影响范围**：09-20 至 11-04 之间用 `install.sh` 安装的 Docker 部署；以及 11-04 之前应用过 k3s 清单、之后没有重新应用 ConfigMap 的集群。09-20 之前安装的部署 `.env` 里没有这一行，更新到含 `c7e4207` 的版本后会自动修好。你这台 Mac 上的 `.env.example` 仍是旧值，可作为对照。

**用户立即可做的修复**

```bash
# Docker 部署：在 Hub 服务器的 CodePier 目录
grep '^OAUTH_REDIRECT_HOSTS=' .env
# 在列表中加入 claude.ai 后重建容器
docker compose up -d

# k3s
kubectl -n <命名空间> edit configmap <codepier 配置>   # OAUTH_REDIRECT_HOSTS 加入 claude.ai
kubectl -n <命名空间> rollout restart deployment/<hub>
```

防火墙或 WAF 若有来源限制，还需放行 Anthropic 出站网段 `160.79.104.0/21`（发现、注册与令牌请求都来自这里）。

**代码修复一：更新与重装时迁移「未改动的旧默认值」**——只替换与历史默认值逐字相同的行，绝不改用户自定义列表（已测试：旧默认、带引号 + CRLF 会被迁移；自定义列表、已是新值、未设置三种情况保持不变）

```python
# scripts/panel_update_runtime.py
OAUTH_OLD_DEFAULTS = {'chatgpt.com,chat.openai.com,platform.openai.com,localhost,127.0.0.1,::1'}  # 55fb590 .. c7e4207
OAUTH_CURRENT = 'chatgpt.com,chat.openai.com,platform.openai.com,claude.ai,localhost,127.0.0.1,::1'

def migrate_oauth_redirect_hosts(env: bytes) -> tuple[bytes, bool]:
    lines, changed = env.decode('utf-8').splitlines(keepends=True), False
    for index, line in enumerate(lines):
        match = re.fullmatch(r'OAUTH_REDIRECT_HOSTS=("?)([^"\r\n]*)\1(\r?\n)?', line)
        if match and match.group(2).strip() in OAUTH_OLD_DEFAULTS:
            quote, newline = match.group(1), match.group(3) or ''
            lines[index] = f'OAUTH_REDIRECT_HOSTS={quote}{OAUTH_CURRENT}{quote}{newline}'
            changed = True
    return ''.join(lines).encode('utf-8'), changed

# committed_env() 末尾
content, migrated = migrate_oauth_redirect_hosts(updated_env(before, changes))
if migrated:
    job.setdefault('notes', []).append('oauth_redirect_hosts_migrated')   # 在更新详情里告诉用户
return content
```

```bash
# deploy/install-hub.sh — .env 已存在的分支（重新运行安装脚本时）
old='OAUTH_REDIRECT_HOSTS=chatgpt.com,chat.openai.com,platform.openai.com,localhost,127.0.0.1,::1'
new='OAUTH_REDIRECT_HOSTS=chatgpt.com,chat.openai.com,platform.openai.com,claude.ai,localhost,127.0.0.1,::1'
if grep -qxF "$old" .env; then
  env_tmp=$(mktemp .env.tmp.XXXXXX)
  cp -p .env "$env_tmp"                                  # 保留权限
  awk -v o="$old" -v n="$new" '$0==o{$0=n}1' .env > "$env_tmp"
  mv "$env_tmp" .env
  echo '已为 Claude 回调补充 OAuth 允许主机 claude.ai'
fi
```

**代码修复二：让问题可诊断**

```python
# hub/oauth.py — __init__ 末尾
KNOWN_CLIENT_HOSTS = {'claude.ai': 'Claude', 'chatgpt.com': 'ChatGPT'}
self.redirect_warnings = [name for host, name in KNOWN_CLIENT_HOSTS.items() if host not in self.redirect_hosts]
if self.redirect_warnings and os.getenv('OAUTH_REDIRECT_HOSTS') is not None:
    logger.warning('OAUTH_REDIRECT_HOSTS omits callbacks for: %s', ', '.join(self.redirect_warnings))
    # 同时在面板「连接诊断」展示，并给出需要加入的主机名

# validate_redirect() — 主机不在列表时给出可操作的错误
if not u.hostname or u.hostname not in self.redirect_hosts:
    raise DevError('REDIRECT_HOST_NOT_ALLOWED',
                   f'回调主机 {u.hostname} 不在 OAUTH_REDIRECT_HOSTS 中；部署管理员需在 .env 加入后重启 Hub',
                   400, redirect_host=u.hostname)
```

同时在 `docs/CHATGPT.md`、`docs/PANEL_UPDATE.md` 写明：从 09-20 至 11-04 之间版本升级的部署需要检查这一行。

---

<a id="p16"></a>
### P16 🟠 协作附件：大对象存主库、每次 Range 都整文件解密

**位置**：`hub/collaboration/channel_media.py:306`（写入）、`channel_media.py:376-385`（读取）、`hub/collaboration/api.py:55-66`（下载接口）

- 附件单个最多 32 MB（`MAX_MEDIA`），写入时先 `base64` 再用 Fernet 加密（Fernet 内部再 base64 一次），以约 1.78 倍体积存进 `hub.sqlite3` 的**一个 TEXT 字段**。缓存上限 256 MB 按原始大小计，实际占库约 456 MB；7 天过期后置 NULL，但 SQLite 文件不会自动缩小，备份也随之变大。
- 下载接口在 `store.run()` 里调用 `content()`：**每个请求**（包括浏览器拖动视频进度条发出的每个 Range 请求）都把整行解密、整个文件重算 SHA-256，然后才切片返回。这些都发生在 Hub 唯一的数据库线程上，一次 32 MB 附件约耗时数百毫秒，期间所有 MCP 调用排队。

第四轮补充：Telegram 外发附件（`telegram_files.py`，单个最大 50 MB）同样会先整体读进 `channel_attachments.content`，一行约 89 MB 的 TEXT。

**修复**（按改动量从小到大）

1. 立即：授权与取行留在 `store.run()`，解密、校验、切片移到 `asyncio.to_thread()`；去掉内层 base64，直接加密原始字节。
2. 中期：附件以 1 MiB 为单位用 AES-GCM 分块加密存到 `HUB_DATA_DIR/attachments/` 文件里，库里只存元数据与每块哈希；Range 只解密命中的块。

```python
# hub/collaboration/api.py — 第一步
@router.get('/attachments/{identifier}/content')
async def attachment_content(request: Request, identifier: str):
    row = await store.run(lambda: service.channels.media.row(identifier, attachment_actor(request)))
    content, start, end, partial = await asyncio.to_thread(
        service.channels.media.decode_range, row, request.headers.get('range'))
    ...
```

---

<a id="p17"></a>
### P17 🟡 协作后台循环与面板总览挤占数据库线程

Hub 所有数据库操作都在一个工作线程上串行执行（`hub/store_worker.py`，`max_workers=1`）。协作模块启用后：

- `lifecycle.py:27-41` 起 7 个循环：对账每 5 秒，事件 2 秒，微信轮询/投递、Telegram 轮询、渠道镜像每 1 秒，媒体 2 秒。没有绑定任何账号时它们也每秒进入一次数据库线程。
- 对账里 `coordination.reconcile()` 对最多 100 个活跃目标的**每个参与者**跑完整的 `authorize(active=True)`（刷新身份、遍历批准历史、逐项目核对快照与能力），`service.reconcile()` 对最多 200 个任务调用 `agent()` → `grant_principal()`。
- 面板总览 `service.read(kind='overview')`（`service.py:973-1028`）一次取 6 个列表各最多 `limit` 条；`jobs` 每条调用 `delivery_status()`（含订阅循环与 `events.authorize`），`agents` 每条调用 `grant_principal()` 加 3 次聚合查询——典型 N+1，且面板会轮询它。

**优化**

- 对账只在有变化时做重授权：`Store._security_write()` 已经维护 `session_revision`（权限相关表写入时递增）。记录上次对账时的 revision，未变化且目标版本未变时跳过参与者重授权。
- 渠道循环在没有启用账号时降频到 30 秒，账号启用时通过现有的 wake 机制唤醒。
- 总览把 `delivery_status` 改成一次按 `object_id IN (...)` 批量查询出盒与投递状态；`agent_view` 的三个聚合改成 `GROUP BY assignee_agent_id`；同一请求内缓存 `grant_principal()` 结果。

---

<a id="p18"></a>
### P18 🟡 协作流水表无保留期；`telegram_inbox` 缺索引

协作模块共 133 张表，`hub/collaboration/retention.py` 只是准入/重试策略对象，不做清理。除附件、监控证据、出盒尾部等少数表外，流水类表在整个 Hub 中**没有任何删除路径**，包括：

| 表 | 每行内容 | 建议保留 |
| --- | --- | --- |
| `collaboration_idempotency` | 每次协作写操作的完整响应 | 30 天 |
| `mcp_event_outbox` / `mcp_event_deliveries` | 每个事件及每个订阅的投递记录 | 已确认且超过重放窗口（7 天）后删除 |
| `telegram_inbox` | 每条 Telegram 更新的加密全文 | 已提交 7 天后置空 `body`，90 天删行 |
| `weixin_inbound` / `weixin_inbound_versions` | 每条微信消息及其版本 | 90 天 |
| `collaboration_audit` | 协作审计 | 与 Hub 审计一致（P2 建议 365 天） |
| `room_changes`、`collaboration_room_changes`、`room_mutations`、`dot_consumer_mutations`、`conversation_idempotency` | 变更流与回执 | 30–90 天 |

清理任务挂在现有对账循环上，每次限量删除（例如 500 行），并跳过仍被未完成任务、活跃订阅游标引用的行。

**索引**：Telegram 每条入站更新都会执行 `SELECT COUNT(*) FROM telegram_inbox WHERE account_id=? AND created>?`（日配额）。表只有主键 `(account_id, update_id)`，查询计划为 `SEARCH ... USING INDEX sqlite_autoindex_telegram_inbox_1 (account_id=?)`——要扫该账号全部历史。加索引后变为覆盖索引范围查询（已用 `EXPLAIN QUERY PLAN` 验证）：

```sql
CREATE INDEX IF NOT EXISTS telegram_inbox_account_created ON telegram_inbox(account_id, created);
```

---

<a id="p19"></a>
### P19 🟡 协作目标到期后永远停在 `active`

**位置**：`hub/collaboration/coordination.py:1032-1052`（`reconcile`）

整个代码库没有把 `coordination_goals` 改为到期状态的语句；对账只检查参与者授权与过期租约，不看 `expires_at`。到期目标在使用时会被 `authorize(active=True)` 拒绝（`GOAL_INACTIVE`），所以不会越权，但面板和 `collaboration_goal_read` 一直显示「进行中」，排队中的工作项也不会被收尾。

```python
# coordination.reconcile() 循环开头
if goal['expires_at'] and goal['expires_at'] <= now:
    self.stop_work(goal, 'GOAL_EXPIRED')
    self.store.execute("UPDATE coordination_goals SET state='expired',version=version+1,updated=? WHERE id=?", (now, goal['id']))
    continue
```

需要同步把 `expired` 加入目标状态的契约枚举与面板展示。

---

<a id="p20"></a>
### P20 ⚪ `next()` 无默认值

`coordination.py:298`、`delegation.py:411-412`、`room_goal_handoffs.py:160`、`room_goal_recovery.py:229,246`、`room_goal_scopes.py:175`、`room_runtime.py:312`、`room_weixin_native.py:382`、`room_consumers.py:308` 在已批准名单里查找成员（或成员项目对应的环境）时用了 `next(生成器)`。逐行核对后，`room_goal_handoffs.py:160` 和 `room_goal_scopes.py:175` 前面已有校验保证一定能找到，只是写法问题；其余几处没有这种保证。`room_goal_recovery.py:246` 在 `except DevError` 里调用，`StopIteration` 不会被捕获，会让整个工作视图返回 500。名单与当前成员不一致时（例如批准后新加入的成员尝试继续工作）会抛 `StopIteration`：在普通函数里变成 500，在协程里被转成 `RuntimeError`，用户看不到可操作的原因。统一改为：

```python
recipient = next((m for m in spec['members'] if m['member_id'] == member['id']), None)
if recipient is None:
    raise DevError('ROOM_MEMBER_NOT_IN_APPROVAL', '当前成员不在这项工作的原批准名单中', 403)
```

---

<a id="p21"></a>
### P21 🔴 聊天记录与工作列表反复全表扫描最大的几张表

**位置**：`chatroom.py:208-222`（`views`）→ `dot_relay.py:106-120,137`（`receipts_many`，默认走批量分支）、`channel_router.py:682-700` 与 `weixin_delivery.py:387-391`（`for_messages`）

一页消息（`conversations.read` / `chatroom.read`，面板与 MCP 共用）在唯一的数据库线程上执行下面这些查询。我用真实迁移后的 441 个库对象建库，逐条做了 `EXPLAIN QUERY PLAN`：

| 查询 | 条件 | 现有索引 | 计划 |
| --- | --- | --- | --- |
| 最新回复 | `collaboration_messages WHERE reply_to_id IN (...)` | `dot_wake_reply_source` 以 `room_id` 开头，用不上 | **SCAN collaboration_messages** |
| dot 回执 | `collaboration_dot_messages WHERE message_id IN (...)` | `UNIQUE(dot_id,message_id)`，前缀不匹配 | **SCAN collaboration_dot_messages** |
| 微信投递 | `weixin_deliveries WHERE message_id IN (...)` | `UNIQUE(binding_id,...)` | **SCAN weixin_deliveries** |
| 镜像投递 | `channel_mirrors WHERE message_id IN (...)`（页内消息一次、回复再一次） | `UNIQUE(channel,binding_id,...)` | **SCAN channel_mirrors ×2** |

`collaboration_messages` 是协作模块里最大的表。实测（内存库、20 万条消息、每页 50 条）：最新回复这一条查询 **61.7 ms**，加索引后 **0.81 ms**。换成磁盘库、再加上另外三张表，翻一页就要在数据库线程上耗掉几百毫秒，这段时间里所有 MCP 调用、事件投递、渠道轮询都只能排队。

同类的缺索引查询还有几处，都在高频路径上：

- `task_liveness.recover()`（`task_liveness.py:306`）：事件泵每 ≤2 秒调用一次，在**写事务**里按 `work_item_id` 关联 `delegation_requests`，没有索引，全表扫。`dot_mailbox.py:349,385,501` 同样按 `work_item_id` 查。
- `channel_router.queue()`（`channel_router.py:185`）：**每条新消息的写事务里**执行 `COUNT(*) FROM channel_mirrors WHERE binding_id=? AND created>?`，唯一索引以 `channel` 开头，全表扫。
- `channel_router.listing()`（`:670`）按 `room_id, conversation_id` 查镜像列表；`dot_chat.receipts()`（`:145`）按 `message_id` 查回执；`coordination.detail()`（`:432`）按 `goal_id` 查目标消息——全部全表扫。

第四轮补充：

- **翻页的第 5 次全表扫**：`receipts_many()` 还会调用 `message_handling.summaries()`（`message_handling.py:293-320`），再按 `message_id IN (...)` 扫一遍 `collaboration_dot_messages`。`message_handling.py:242` 与 `owner_task_context.incoming()`（每条已验证的本人入站消息）也按 `message_id` 查这张表。
- **工作列表的乘数效应**：`work_view()` 对每个目标调用 `room_diagnostics.work_deliveries()`（`room_diagnostics.py:48-72`），后者调用 `channels.for_messages()`，每次全表扫 `weixin_deliveries` 和 `channel_mirrors` 各一遍。`work_page()` 最多检查 1001 个目标（见 P22），一次房间读取或房间写操作的返回值，最多就是约 2000 次全表扫描。
- **回显检测**：`weixin.py:433` 和 `room_weixin_native.py:493-495` 对每条带 `client_id` 的入站消息（机器人自己发出消息的回显）执行 `WHERE 'cpm-' || id=?`。对列做表达式后用不上主键，`channel_mirror_parts` 与 `channel_onboarding_notices` 每次都全表扫。
- **成员与工作查找**：`dot_consumers.room_member()`（每次受保护的 dot 操作都会调用）按 `legacy_dot_id` 全表扫 `room_members`，并逐行执行 `EXISTS`；`room_native_work_assignments` 按 `member_id`、`room_work_sources` 按 `goal_id`、`room_member_work_bridges` 按 `policy_id`、`channel_owner_messages` / `room_channel_owner_inputs` 按 `authorization_id` 查找，也都没有索引。

**修复**（在 `dot_chat.migrate()` 或新的迁移函数里追加，均已验证计划变为 `SEARCH ... USING INDEX`）：

```sql
CREATE INDEX IF NOT EXISTS collaboration_messages_reply    ON collaboration_messages(reply_to_id, created);
CREATE INDEX IF NOT EXISTS dot_messages_message            ON collaboration_dot_messages(message_id, sequence);
CREATE INDEX IF NOT EXISTS weixin_deliveries_message       ON weixin_deliveries(message_id, created);
CREATE INDEX IF NOT EXISTS channel_mirrors_message         ON channel_mirrors(message_id);
CREATE INDEX IF NOT EXISTS channel_mirrors_scope           ON channel_mirrors(room_id, conversation_id, created);
CREATE INDEX IF NOT EXISTS channel_mirrors_binding_created ON channel_mirrors(binding_id, created);
CREATE INDEX IF NOT EXISTS delegation_requests_work        ON delegation_requests(work_item_id);
CREATE INDEX IF NOT EXISTS coordination_messages_goal      ON coordination_messages(goal_id, created);
-- 第四轮补充
CREATE INDEX IF NOT EXISTS room_members_legacy_dot         ON room_members(legacy_dot_id);
CREATE INDEX IF NOT EXISTS room_work_bridges_policy        ON room_member_work_bridges(policy_id);
CREATE INDEX IF NOT EXISTS room_native_assignments_member  ON room_native_work_assignments(member_id);
CREATE INDEX IF NOT EXISTS room_work_sources_goal          ON room_work_sources(goal_id);
CREATE INDEX IF NOT EXISTS channel_owner_messages_auth     ON channel_owner_messages(authorization_id);
CREATE INDEX IF NOT EXISTS room_channel_owner_inputs_auth  ON room_channel_owner_inputs(authorization_id);
```

`dot_consumers.room_member()` 里的 `legacy_dot_id=? OR EXISTS(...)` 即使有索引也仍是 SCAN（已验证），需要同时改写成可以走索引的形式：

```sql
SELECT m.* FROM room_members m
WHERE m.status!='legacy' AND m.id IN (
  SELECT id FROM room_members WHERE legacy_dot_id=?
  UNION SELECT member_id FROM room_member_work_bridges WHERE legacy_dot_id=?)
LIMIT 1
```

回显检测改为先在 Python 里去掉前缀，再按主键查：

```python
def echo(store, client_id):
    if not isinstance(client_id, str) or not client_id:
        return False
    if store.one('SELECT 1 FROM weixin_parts WHERE client_id=?', (client_id,)):
        return True
    for prefix, table in (('cpo-', 'channel_onboarding_notices'), ('cpm-', 'channel_mirror_parts')):
        if client_id.startswith(prefix) and store.one(f'SELECT 1 FROM {table} WHERE id=?', (client_id[4:],)):
            return True
    return False
```

防止回归：把附录 A.6 的 `scripts/check_query_plans.py` 放进 CI。它用真实迁移结果建库，对 `hub/` 里所有字面量 SQL 做 `EXPLAIN`，发现对「随历史增长的表」做全表扫就失败。当前代码上它报出 25 处（另含 `api/activity.py`、`call_log.py`、`runtime.py:481` 对 `operations`/`audit` 的扫描）。拼接出来的 SQL（如上面的 `IN (...)`）它看不到，需要人工补用例。

---

<a id="p22"></a>
### P22 🟠 房间读取被放大：每次房间操作都重算上百次完整授权

**位置**：`room_runtime.py:632-668`（`work_page`）、`:683`（`snapshot`）、`:381-430`（`read`）；`rooms.py:243-256`（`member_view`）、`:343-357`（`hint_snapshot` / `hint_stream`）

- `work_page()` 为了凑够 100 条可见工作，最多扫描 1001 个目标，对每个调用 `work_view()`：完整的 `coordination.authorize` + 操作列表 + 投递状态 + 恢复投影。`snapshot()` 调 `work_page(100)`，`rooms.view()` 调 `snapshot()`，而**每个房间写操作**（创建、改名、加项目、加成员、连接）的返回值都是 `rooms.read()` → `view()`。`room_runtime.read()` 也会再调一次 `work_page(100)`。忙碌的房间里点一下按钮，就是几百次完整授权。
- `room_runtime.read()` 最多扫描 `min(1000, limit*10+1)` 条消息，逐条 `message_visible()` → 对每个来源项目调 `runtime.project()`，同一请求内没有缓存。
- `member_view()` 先算 `state()`（内部已调用 `scopes()`），随后又调用一次 `scopes()`；每个 scope 都要刷新身份、核对项目、目标快照与能力。
- `hint_stream()`：每个 SSE 连接在每次唤醒（任何审计、事件、变更提交后）和每 5 秒，都对房间所有成员重算 `state()`。打开的面板标签越多，压力成倍增加。

第四轮补充的几处同类放大：

- **聊天视图**：`chatroom.view()` 对每条外部渠道消息调用 `channel_owner.author()`，进而走 `trusted()` / `live()`（校验绑定、刷新身份、`grant_reader`、`approved_owner`、`authorize_policy`）或原生房间的 `author()` / `preview()`。一页 50 条微信消息就是 50 次完整授权。
- **房间上下文**：`room_context.project()`（成员读取上下文、面板读取上下文）开头算了一次 `projects()`，但 `fact()` 和 `source()` → `source_visible()` 又各自重算。成员路径下，每次 `projects()` 都对每个 scope 做完整授权。最多 100 条事实、每条若干来源，再加最近 40 条消息各一次，同一请求里要重复几百次。循环里每加一条还要把整个结果重新序列化一次来量字节数。
- **恢复投影**：`work_view()` 对房主调用 `room_goal_recovery.projection()`，后者对房间里每个成员、两种动作各跑一次 `plan()`（完整候选授权）。这和 `work_page()` 最多 1001 个目标相乘。

修复方向相同：同一请求内把「当前可见范围」和授权结果算一次，往下传。

**优化**

```python
# 1) 写操作不再返回完整快照：返回 revision，前端按需再读
return {'conversation_id': cid, 'revision': revision, 'changed': kind}

# 2) 请求级缓存：同一请求内授权结果只算一次
from functools import lru_cache
class RequestScope:
    def __init__(self, c, principal):
        self.project = lru_cache(maxsize=256)(lambda pid: c.runtime.project(pid, principal))

# 3) work_page 先用 SQL 过滤（状态、成员可见的项目集合），再对候选做授权；扫描上限降到 limit*2
# 4) member_view 复用 state() 里已算出的 scopes
# 5) hint_stream 以 conversation 为单位缓存 hint_snapshot，按 revision 失效，多个连接共享
```

---

<a id="p23"></a>
### P23 🟠 附件缓存配额全站共享：一个用户 32 次上传就能让所有人收不了附件

**位置**：`channel_media.py:22,246-250,276-278,301-304`；`channel_media_router.py:120-131,158,196,267`；`room_attachments.py:22,43-55`

附件缓存上限 `MAX_CACHE = 256 MiB` 是**全站一个总数**：配额检查 `SUM(size) FROM channel_attachments WHERE state='ready'` 和 `SUM(bytes) FROM channel_attachment_chunks` 都没有按空间或用户过滤。

面板上传（`room_attachments.upload`）单个最大 8 MiB，上传后立即是 `ready`，但只挂在 `pending:<id>` 上，**不发消息也会保留 7 天**并计入总量。任何房主连续上传 32 个 8 MiB 文件，全站所有用户的微信、Telegram 入站附件和面板上传都会拿到 `ATTACHMENT_CACHE_QUOTA` / `ATTACHMENT_CACHE_LIMIT`，持续 7 天。一个发图很多的微信联系人也能产生同样效果。另外，这几个 `SUM` 在每次领取、每个分段写入时都全表扫描。

**修复**

```python
PER_OWNER_CACHE = 64 * 1024 * 1024      # 每个用户的份额
UNATTACHED_TTL = 24 * 3600              # 未发送的面板上传 24 小时后过期

def cache_usage(self, space_id, owner_user_id, exclude_chunks=None):
    ready = self.store.one("""SELECT COALESCE(SUM(size),0) AS n FROM channel_attachments
        WHERE space_id=? AND owner_user_id=? AND state='ready'""", (space_id, owner_user_id))['n']
    chunks = self.store.one("""SELECT COALESCE(SUM(c.bytes),0) AS n FROM channel_attachment_chunks c
        JOIN channel_attachments a ON a.id=c.attachment_id
        WHERE a.space_id=? AND a.owner_user_id=? AND c.attachment_id!=?""",
        (space_id, owner_user_id, exclude_chunks or ''))['n']
    return ready + chunks
```

```sql
CREATE INDEX IF NOT EXISTS channel_attachments_owner_usage ON channel_attachments(space_id, owner_user_id, state, size);
```

全站总上限保留，作为第二道保护；面板上传的过期时间在 `attach()` 成功后再延长到 7 天。

同样的「全站共享上限」还有一处：原生房间登记微信账号时，除每用户 32 个外，还检查 `SELECT COUNT(*) FROM weixin_accounts` 不超过 256（`room_weixin_native.py:200-203`），不分空间和用户。8 个用户登满后，其他人都无法再接入微信。建议去掉全站总数，或改成管理员可配置的值。

---

<a id="p24"></a>
### P24 🟠 两条归档路径互不知情，恢复时直接 500

**位置**：`rooms.py:419-454`（`update`）；`conversation_archive.py:325-336`（`restore`）及 `archive`

| 动作 | `rooms.update(state='archived')` | `conversation_archive.archive` |
| --- | --- | --- |
| `conversation_rooms.state` | 设为 `archived` | **不设** |
| 归档快照 `conversation_archives` | **不写** | 写 |
| 取消任务与目标、撤销微信/Telegram 绑定、关闭发言权限、清理项目关系 | **都不做** | 做 |
| 成员策略暂停、取消订阅 | 做 | 做 |

后果：

1. 用 `rooms.update` 归档的房间，再用 `conversation_archive.restore` 恢复时，`row` 为 `None`，`json.loads(row['snapshot'])` 抛 `TypeError`，接口返回 500。
2. 反过来用 `rooms.update(state='active')` 解除归档，会直接清掉 `archived_at`，绕过 `restore()` 对项目是否仍可用的检查。
3. 渠道绑定在库里仍是 `active`，但轮询时 `valid()` 抛 `CONVERSATION_ARCHIVED` 被静默过滤。对方发来的消息被归为 `unbound_or_ambiguous_private_chat` 隔离，房主看不到任何提示。

**修复**：归档只保留一条路径。

```python
# rooms.update()
if args.get('state') == 'archived' and room['state'] != 'archived':
    return self.c.conversation_archive.archive({'conversation_id': room['id'], **receipt_args}, principal)
if args.get('state') == 'active' and room['archived_at']:
    return self.c.conversation_archive.restore({'conversation_id': room['id'], **receipt_args}, principal)

# conversation_archive.archive(): 同时写状态
self.store.execute("UPDATE conversation_rooms SET state='archived',archived_at=?,version=version+1 WHERE id=?", (now, cid))

# conversation_archive.restore(): 缺快照时给出可操作的错误
if not row:
    raise DevError('ARCHIVE_SNAPSHOT_MISSING', '这个房间由旧路径归档，没有归档快照；请重新选择要恢复的项目', 409)
```

再加一次性迁移：找出 `state='archived'` 但没有快照的房间，补写快照，并撤销其渠道绑定。

---

<a id="p25"></a>
### P25 🟠 后台泵只认识几种异常：一条坏数据拖垮整轮投递

**位置**：`events.py:884-901`（`deliver`）、`:903-960`（`tick`）、`:596-603,708-716`；`channel_router.py:577-592,650-666`

- `events.deliver()` 只捕获 `DevError`、`TimeoutError`、`httpx.HTTPError`、`OSError`。`tick()` 对完成的任务调用 `task.result()`，其他异常（订阅密钥解密的 `InvalidToken`、IDNA / `getaddrinfo` 的 `UnicodeError`、`KeyError` 等）会直接抛出 `tick()`。`finally` 随即取消所有正在进行的 HTTP 投递，这些投递结果变成「不确定」，要等 30 秒租约过期后重发。下一轮又领到同一条坏投递，循环往复。`subscribe()` 捕获了 `ValueError`/`UnicodeError`，`deliver()` 却没有。
- `discover_deliveries()` / `reserve()` 对 `authorize()` 抛出的任何 `DevError`（除 3 个白名单外）都**永久撤销订阅**，不区分临时与永久原因。
- 渠道镜像 `send_once()` 在真正发送前只捕获 `DevError`：`claim()` 或 `outbound_content()` 抛出其他异常时，分项停在 `preparing`，180 秒后被对账放回队列，再次失败——永远循环。`tick()` 还是**串行**发送最多 16 个分项（Telegram 附件最大 50 MB），一个慢上传会阻塞所有绑定的镜像。

**修复**

```python
# events.deliver()
except (TimeoutError, httpx.HTTPError, OSError):
    await self.store.run(self.finish, item, None, 'network_error')
except Exception as exc:  # 未预期错误只影响这一条投递
    logger.warning('event delivery failed (%s)', type(exc).__name__)
    await self.store.run(self.finish, item, None, 'delivery_internal_error')

# events.tick()
for task in completed:
    if not task.cancelled() and task.exception() is not None:
        logger.warning('event delivery task crashed (%s)', type(task.exception()).__name__)

# 订阅撤销：只对表示「权限已不存在」的错误撤销；冲突、限流、暂停类错误只跳过本轮
if exc.code in {'ROOM_PAUSED', 'WORKER_BINDING_REQUIRED', 'GOAL_INACTIVE'} or exc.status in {409, 429} or exc.status >= 500:
    continue   # 保持订阅，下一轮重试；原因写入 health / 诊断（表里目前没有 reason_code 列，需要新增）
self.store.execute("UPDATE mcp_event_subscriptions SET state='revoked',version=version+1,updated=? WHERE id=?", (now, sub['id']))

# channel_router.tick()：与微信投递一致，按绑定并发、上限 4
semaphore = asyncio.Semaphore(4)
async def run(row):
    async with semaphore:
        await self.send_once(row['id'])
await asyncio.gather(*(run(r) for r in rows), return_exceptions=True)
```

---

<a id="p26"></a>
### P26 🟠 「待处理」集合只进不出，每轮都全量重扫

五处同样的模式：条目进入某个「待处理」状态后再也出不来，而后台循环每轮都把它们全部重新处理一遍。成本随历史线性增长。

| 位置 | 只进不出的东西 | 每轮做什么 | 频率 |
| --- | --- | --- | --- |
| 旧版 dot 邮箱 `dot_mailbox.py:554-580,714-770,772` | `message` 类条目：`ack()` 只接受 `peer_handoff`/`result_ready`/`control_notice`，消息条目永远不确认，也不删除 | `has_ready()` 扫描全部未确认条目，逐条取消息、做可见性检查（每个来源项目一次 `runtime.project`）；`read()` 的续读请求不带游标、SQL 也不过滤已确认，每次唤醒都从序号 0 开始翻空页 | `recover()` 每 5 秒，最多 64 个绑定 |
| 原生房间邮箱 `room_consumers.py:353-397` | 同上，`inbox()` 的 SQL 没有过滤已确认 / 已被替代 | 每次唤醒从 0 翻页 | 每次唤醒 |
| 镜像对账 `channel_router.py:366-381` | 结果为 `unknown` 的分项（需房主人工确认，通常没人点） | 对**每个**含 `unknown` 分项的投递执行一次 `SELECT` + `UPDATE channel_mirrors` | **每 1 秒** |
| 监控证据 `monitor.py:367,395-401` | 每次规则评估插入一行 `monitor_evidence`，过期后只把 `body` 置为 `'null'`，行永不删除 | `cleanup()` 的 `UPDATE` 全表扫描（`expires_at` 无索引）加 `NOT IN` 子查询；`monitor_samples` 按 `collected_at` 删除也是全表扫 | 每 2 秒 |
| 委托待办 `delegation_consumer.py:139-155,282-290` | 该委托策略下的全部历史工作 | 每次唤醒或翻页都取出全部历史工作，逐行用相关子查询在 `mcp_event_outbox` 里找最大序号（含 `json_extract`），再在 Python 里过滤分页；`status()` 也扫全部历史和全部委托事件 | 每次唤醒 |

另有一处一次性的同类开销：在面板关闭某个渠道时，`channel_settings.pause()`（`:271-296`）对每个绑定遍历**全部历史镜像**，并逐条 `refresh()`（写库）；`channels.quarantine()` 又遍历一次。历史越长，这个写事务占住数据库线程越久。

后果是 dot 越用越慢：模型每次被唤醒要花 N/limit 次工具调用去翻空页，数据库线程上的对账开销也持续增长。

**修复**

```python
# dot_mailbox.read()：只读未结清的条目（已有索引 dot_mailbox_entries_ready 可用）
WHERE dot_id=? AND generation=? AND sequence>? AND acknowledged_at IS NULL AND superseded_by=''
# room_consumers.inbox()：同样的过滤；已有部分索引 room_mailbox_pending 正好覆盖
WHERE member_id=? AND generation=? AND sequence>? AND acknowledged_at IS NULL AND superseded_by=''
# 两者的续读请求都带上最后一个序号作为游标，而不是每次从 0 开始

# 旧版邮箱：消息处理完成（message_handling 记录 handled / dot 回复完成）时顺带结清
self.store.execute("""UPDATE dot_mailbox_entries SET acknowledged_at=? WHERE dot_id=? AND kind='message'
    AND object_id=? AND acknowledged_at IS NULL""", (now, dot_id, message_id))

# 镜像对账：只刷新这一轮刚变成 unknown 的投递
rows = self.store.all("SELECT DISTINCT delivery_id FROM channel_mirror_parts WHERE state='sending' AND lease_until<=?", (now,))
self.store.execute("""UPDATE channel_mirror_parts SET state='unknown',reason_code='send_interrupted'
    WHERE state='sending' AND lease_until<=?""", (now,))
for row in rows:
    self.refresh(row['delivery_id'])
# refresh() 只在状态真的变化时写 channel_mirrors：UPDATE ... WHERE id=? AND state!=?
```

```sql
-- 监控：加索引；墓碑 30 天后删除（参数为 now - 30 天）
CREATE INDEX IF NOT EXISTS monitor_evidence_expiry   ON monitor_evidence(expires_at);
CREATE INDEX IF NOT EXISTS monitor_samples_collected ON monitor_samples(collected_at);
DELETE FROM monitor_evidence WHERE id IN (SELECT id FROM monitor_evidence WHERE body='null' AND expires_at<? LIMIT 500)
  AND id NOT IN (SELECT evidence_id FROM monitor_incidents WHERE evidence_id IS NOT NULL);
```

---

<a id="p27"></a>
### P27 🟡 消息搜索在 JSON 原文上做 `LIKE`

**位置**：`chatroom.py:603-610`、`conversations.py:336-342`

搜索条件是 `body LIKE '%关键词%'`，而 `body` 是整条消息的规范化 JSON（含 `mentions`、`notifications`、`request_digest` 等字段，引号、换行都被转义）。复现（附录 A.7）：

| 搜索词 | 现在的结果 | 修复后 |
| --- | --- | --- |
| `"v2"`（正文含引号） | 0 条 | 命中 |
| 含换行的一段话 | 0 条 | 命中 |
| `mentions` | **所有消息** | 0 条 |
| `queued`、摘要的十六进制片段 | 命中元数据 | 0 条 |

**修复**：只搜正文。

```python
clauses.append("json_extract(body,'$.body_text') LIKE ? ESCAPE '\\'")
```

消息量大以后建议改用 FTS5 外部内容表（`content='collaboration_messages'`），由现有的 `collaboration_message_insert/change` 触发器同步。

---

<a id="p28"></a>
### P28 🟡 微信回复的日上限静默丢弃，且把已送达的也算进去

**位置**：`weixin_delivery.py:78-83`

变量名是 `pending`，但查询统计的是该账号 24 小时内**创建的全部投递**（包括已送达的）。超过 1000 条后 `queue_reply()` 直接 `return None`：不写投递记录、不记原因，房主在面板上看不到这条回复为什么没发到微信。完整镜像的日上限（`channel_router.py:183-231`）会写一条 `rejected / daily_mirror_quota` 分项，两者应保持一致。

```python
pending = self.store.one("""SELECT COUNT(*) AS n FROM weixin_deliveries d JOIN weixin_bindings b ON b.id=d.binding_id
    WHERE b.account_id=? AND d.created>? AND d.state NOT IN ('accepted','owner_confirmed','cancelled')""",
    (account['id'], now - 86400))['n']
if pending >= 1000:
    identifier = self._insert_delivery(binding, message, source)   # 把第 85-89 行的 INSERT 抽成函数
    self.add_part(identifier, 0, 'text', {'text': ''})
    self.store.execute("UPDATE weixin_parts SET state='rejected',reason_code='daily_reply_quota' WHERE delivery_id=?", (identifier,))
    self.refresh(identifier)                                          # 投递状态变为 needs_attention，面板可见
    return self.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (identifier,))
```

---

<a id="p29"></a>
### P29 🟠 实时提示触发器把每次操作写入放大成整个项目的面板刷新

**位置**：`hub/collaboration/relay_hints.py:11-40`；写入来源 `operation_output.py:82`、`runtime.py:1422-1452`；前端 `web/collaboration-room.js:2630`

迁移时会在 13 张表上为 INSERT / UPDATE / DELETE 各建一个触发器，每次写入都给对应房间的 `collaboration_relay_revisions.revision` 加一。问题在 `operations`：

- 触发器不限列：输出每到一段（更新 `output_seq`）、每次心跳（`last_operation_verified_at`）、每次重试排期（`next_attempt`）都会触发。
- 触发器里执行 `SELECT id FROM collaboration_rooms WHERE project_id=NEW.project_id`。现有索引以 `space_id` 开头，计划是 **SCAN collaboration_rooms**，然后对该项目下每个房间做一次 UPSERT。
- 普通 MCP 用法（与协作无关的 `read`、`exec`）同样触发。
- revision 一变，面板 SSE 的下一轮快照就推送 `change`，`collaboration-room.js` 收到后 `wake()`，重新读取房间——也就是 P22 里那份很重的快照。只要项目里有命令在输出，打开的协作页面就会每隔几秒整页重算一次。

`mcp_event_deliveries`（租约、重试）和 `coordination_work`（心跳、进度）也有同样的「任意列都触发」。

**修复**（已在真实库结构上编译，计划变为 `SEARCH ... USING INDEX`）：

```sql
CREATE INDEX IF NOT EXISTS collaboration_rooms_project ON collaboration_rooms(project_id);

DROP TRIGGER IF EXISTS relay_hint_operations_update;
CREATE TRIGGER relay_hint_operations_update AFTER UPDATE OF state ON operations
WHEN OLD.state IS NOT NEW.state BEGIN
  INSERT INTO collaboration_relay_revisions(room_id,revision)
  SELECT id,1 FROM collaboration_rooms WHERE project_id=NEW.project_id
  ON CONFLICT(room_id) DO UPDATE SET revision=revision+1;
END;
-- mcp_event_deliveries、coordination_work 同理，改为 UPDATE OF state（work 可加 version）
```

前端再加一层：同一房间的 `change` 在 1–2 秒内合并成一次刷新。

---

<a id="p30"></a>
### P30 🟠 撤销项目授权会把已结束的目标改回「暂停」

**位置**：`hub/collaboration/project_scopes.py:240-252`

撤销某个项目授权时，代码遍历本会话全部 `delegation_requests`，对引用了该授权的每个目标执行 `stop_work()`，然后**无条件**执行 `UPDATE coordination_goals SET state='paused'`。已经 `cancelled`、`expired` 或已完成的目标也会被改成 `paused`。

其他几条停止路径都有状态判断：

| 路径 | 判断 |
| --- | --- |
| `room_weixin_native.revoke_tasks` | `not in {'cancelled','expired'}` |
| `room_channels.stop_legacy` | `in {'active','paused'}` |
| `working_scopes.apply` | `in {'active','proposed'}` |

后果是「复活」：`room_runtime` 的 `resume` 只要求 `state=='paused'`（再加一次有效期授权检查），`coordination` 的重新批准只拒绝 `cancelled` 状态。一个已取消、批准仍在有效期内的目标，在撤销项目授权后可以被恢复执行。

**修复**：

```python
if goal['state'] in {'active', 'proposed'}:
    self.c.coordination.stop_work(goal, 'PROJECT_SCOPE_REVOKED')
    self.store.execute("""UPDATE coordination_goals SET state='paused',version=version+1,updated=?
        WHERE id=? AND state IN ('active','proposed')""", (self.c.clock(), goal['id']))
```

顺带把同一循环改成按 `conversation_id` + JSON 条件在 SQL 里筛选，避免每次撤销都读出会话全部委托历史。

---

<a id="p31"></a>
### P31 🟡 大文件外发用固定截止时间

**位置**：`telegram_protocol.py:43-72`（`call()` 固定 `timeout=40`，`sendDocument` 也走这里）；`weixin_protocol.py:262`（CDN 上传 `timeout=60`）；`weixin_protocol.py:126-131`（`public_transport` 用 `asyncio.timeout(timeout)` 包住整个请求）

| 通道 | 单文件上限 | 截止时间 | 需要的上行速率 |
| --- | --- | --- | --- |
| Telegram `sendDocument` | 50 MB | 40 秒 | ≥ 10 Mbit/s |
| 微信 CDN 上传 | 32 MB | 60 秒 | ≥ 4.3 Mbit/s |

Hub 部署在家庭宽带或小 VPS 时上行常低于这个值。Telegram 超时后抛 `TELEGRAM_TRANSPORT_UNKNOWN`，分项进入 `unknown`。按设计 `unknown` 不会自动重发，只能由房主人工确认；而文件可能已经送达，也可能没有。

**修复**：截止时间按大小计算，例如 `timeout = 30 + size / (256 * 1024)`（按 2 Mbit/s 估算，上限 15 分钟）；`public_transport` 的整体截止和写超时分开设置。面板在批准外发时提示预计耗时。

---

<a id="p32"></a>
### P32 🟡 配对窗口内，陌生人每发一条消息机器人都回一条提示

**位置**：`channel_enrollment.py:216-244`（`incoming(matched=False)` → `notices.queue('pairing_required')`）；调用方 `telegram.py:596-608`、`weixin.py:445`

预授权接入的 15 分钟配对窗口内，如果没有指定 `expected_peer_id`，任何人给机器人发的每一条非配对码消息，都会生成一条「尚未完成授权验证…」回执。唯一约束是 `(channel, account, generation, peer, source_external_id, kind)`，也就是每条消息一条，没有按联系人去重或限速。Telegram 机器人谁都能私聊，刷消息就能让机器人持续回复。同一账号的通知按 1.1 秒间隔串行发送，本人的「已连接」回执会排在后面。

**修复**：同一绑定、同一联系人在一个配对窗口内最多一条 `pairing_required`；没有指定 `expected_peer_id` 时，只对看起来像配对码（`CPW-` 前缀或长度相符）的消息回复。

---

## 3. 对 2026-10-20 扫描报告的复核

`BUG_REPORT_2026-10-20.md` 中几条标为 P0 的条目经核对不成立，建议不要据此改代码：

| 原条目 | 核对结果 |
| --- | --- |
| #2 SQL 注入（`runtime.py` 动态列名） | 列名来自两个代码内常量的二选一，没有外部输入，不可注入 |
| #4 CSRF Token 弱随机性 | `token()` 即 `secrets.token_urlsafe(32)`（256 位 CSPRNG），校验使用 `hmac.compare_digest` |
| #1 会话校验不足 | 会话按 SHA-256 存储，有 `session_security.epoch` 撤销机制与 12 小时过期；IP 绑定对移动网络不友好，属于产品取舍而非缺陷 |
| #3 密码重放 | 未知用户同样执行 dummy scrypt，失败审计记录用户名哈希与 IP；没有看到可重放的路径 |

该报告的修复示例中还有在同步函数里 `await` 的代码（#1），直接合入会导致语法错误。

---

## 4. 落地顺序建议

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| 第 1 批（立即） | P15 迁移脚本 + 诊断提示；P12 渠道按条提交；P1 `Store` 修复（同时修好 `admit`/`stop_work`/团队派发三处）+ AST 检查进 CI；P21 索引 + 查询计划检查进 CI；P29 收窄实时提示触发器 | 老部署连不上 Claude、外部渠道可被一条消息卡死、执行步骤可能脱离预算、翻聊天记录拖慢所有调用——都是用户直接可感知的故障；P21 只加索引，风险最低、收益最大 |
| 第 2 批（本周） | P13 自动基线 + 新鲜度提示 + `workspace_prune`；P14 搜索走 `git ls-files`；P4；P5；P23 按用户分配附件配额；P24 归档收敛为一条路径；P25 投递泵兜底；P30 撤销项目授权不再改动已结束目标 | 让 AI 读到当前代码、找得到代码；安全边界；跨用户互相影响与 500 错误 |
| 第 3 批 | P3 Agent 清理；P2 Hub 保留期；P18 协作保留期与索引；P26 邮箱结清与对账增量化；P16 附件读取移出数据库线程；P22 房间写操作不再返回完整快照 | 磁盘、隐私与数据库线程占用 |
| 第 4 批 | P6；P7；P8；P10；P17；P19；P27；P28；P31；P32 | 性能、加固与小的正确性问题，彼此独立 |
| 随手 | P9；P11；P20 | 低风险清理 |

每批合入前建议跑 `scripts/check_full_regression.py`，P1 额外关注 `tests/` 里协作租约、MCP Events 出盒和设备轮换相关用例。

---

## 附录 A：复现与验证脚本

脚本只依赖仓库本身和标准库（A.2、A.3、A.4 需要 `cryptography`、`PyYAML`，项目虚拟环境已有）。把 `REPO` 改成仓库路径后用 `python -I` 运行。

### A.1 P1 事务提前提交

```python
import os, sqlite3, tempfile, threading
from contextlib import contextmanager

class Store:  # 与 hub/store.py 的 transaction()/execute() 完全一致
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.lock = threading.RLock()
    @contextmanager
    def transaction(self, *, immediate=True):
        with self.lock, self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
            yield self.db
    def execute(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args)

path = os.path.join(tempfile.mkdtemp(), 'hub.sqlite3')
s = Store(path)
s.db.execute("CREATE TABLE operations(id TEXT PRIMARY KEY, state TEXT)")
s.db.execute("INSERT INTO operations VALUES ('op1','running')"); s.db.commit()
try:
    with s.transaction():
        s.execute("UPDATE operations SET state='succeeded' WHERE id='op1'")
        raise RuntimeError("outbox write failed")
except RuntimeError:
    pass
print(sqlite3.connect(path).execute("SELECT state FROM operations").fetchone()[0])  # succeeded（应为 running）
```

### A.2 P4 通道重放

```python
import sys; sys.path.insert(0, REPO)
from shared.crypto import SecureChannel, token

secret, device, challenge = token(32), "dev1", token(32)
hub = SecureChannel(secret, challenge, device, "hub")
recorded = [hub.pack({"type": "ready"}),
            hub.pack({"type": "native_request", "request_id": "r1", "action": "send", "text": "..."})]
agent = SecureChannel(secret, challenge, device, "agent")   # 新会话，攻击者重放旧 challenge
agent.pack({"type": "hello"})
for packet in recorded:
    print(agent.unpack(packet))                             # 全部被接受
```

### A.3 P5 配置备份可读

```python
import json, sys, tempfile, uuid
from pathlib import Path
sys.path.insert(0, REPO)
from agent.filesystem import FileEngine
from agent.journal import Journal

base = Path(tempfile.mkdtemp()); project = base / "workspace"
cfg_dir = project / "ops" / "codepier"; cfg_dir.mkdir(parents=True)
config = {"device_id": "d", "secret": "S" * 48, "hub_url": "http://h",
          "allowed_roots": [{"path": str(project), "writable": True}]}
(cfg_dir / "config.json").write_text(json.dumps(config))
backup = "config.json.runtime-backup-" + uuid.uuid4().hex
(cfg_dir / backup).write_text(json.dumps(config))
engine = FileEngine(config, Journal(base / "state"), cfg_dir / "config.json")
proj = {"root": str(project), "mode": "write"}
print("S" * 48 in json.dumps(engine.read(proj, {"path": f"ops/codepier/{backup}"})))   # True
```

### A.4 P3 清理函数验证

见 P3 的 `prune()`；验证用例：写入 5 条 40 天前已 ACK 的调用、1 条未 ACK、1 条执行中；同一路径 25 份备份；12 个检查点。期望输出：

```
{'calls': 5, 'backups': 5, 'checkpoints': 2, 'cancellations': 0}
left calls: ['running', 'unacked']
left backup files: 20  checkpoints: 10
```

### A.5 CI 检查：事务块内的提前提交调用

```python
#!/usr/bin/env python3
"""scripts/check_store_transactions.py — fail CI on commit-inducing calls inside a transaction block."""
import ast, pathlib, sys

def opens_txn(item):
    text = ast.unparse(item.context_expr)
    return text.endswith(".transaction()") or ("store" in text and text.endswith(".db"))

hits = []
for root in sys.argv[1:] or ["hub", "shared", "agent"]:
    for file in pathlib.Path(root).rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.With, ast.AsyncWith)) and any(opens_txn(i) for i in node.items):
                for call in (c for stmt in node.body for c in ast.walk(stmt)):
                    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                        continue
                    receiver = ast.unparse(call.func.value)
                    if not receiver.endswith("store"):
                        continue
                    if call.func.attr == "execute" or (call.func.attr == "audit" and
                            not any(k.arg == "commit" for k in call.keywords)):
                        hits.append(f"{file}:{call.lineno}: {ast.unparse(call)[:100]}")
print("\n".join(hits))
sys.exit(1 if hits else 0)
```

P1 修复合入后，`store.execute()` 在事务内会自动加入外层事务，此检查可以改为只告警 `with store.lock, store.db:` 的写法，推动统一到 `store.transaction()`。

### A.6 CI 检查：随历史增长的表不许全表扫描（P21）

在仓库根目录用项目虚拟环境运行。首次运行把现有命中整理进 `scripts/query_plan_baseline.txt`（每行 `文件:SQL 前 120 字`），之后 CI 只拦新增。

```python
#!/usr/bin/env python3
"""scripts/check_query_plans.py — EXPLAIN every literal SQL in hub/ against the real migrated schema."""
import ast, pathlib, re, sqlite3, sys, tempfile
sys.path.insert(0, '.')
from hub.store import Store

GROWING = {
    'collaboration_messages', 'collaboration_dot_messages', 'collaboration_room_changes', 'collaboration_audit',
    'weixin_deliveries', 'weixin_parts', 'weixin_inbound', 'telegram_inbox', 'channel_mirrors', 'channel_mirror_parts',
    'channel_attachments', 'channel_attachment_chunks', 'delegation_requests', 'coordination_work', 'coordination_messages',
    'coordination_operations', 'mcp_event_outbox', 'mcp_event_deliveries', 'dot_mailbox_entries', 'monitor_evidence',
    'monitor_samples', 'operations', 'audit',
}
ALIAS = re.compile(r'(?i)\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(?!WHERE|ON|JOIN|LEFT|INNER|ORDER|GROUP|LIMIT|SET)(\w+))?')

store = Store(tempfile.mkdtemp())
with store.lock:
    schema = [r[0] for r in store.db.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")]
db = sqlite3.connect(':memory:')
for statement in schema:
    db.execute(statement)

baseline_file = pathlib.Path('scripts/query_plan_baseline.txt')
baseline = set(baseline_file.read_text().splitlines()) if baseline_file.exists() else set()
hits = []
for file in sorted(pathlib.Path('hub').rglob('*.py')):
    for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        sql = node.value.strip()
        if not re.match(r'(?is)^(SELECT|UPDATE|DELETE|WITH)\b', sql):
            continue
        try:
            plan = [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + sql, (None,) * sql.count('?'))]
        except sqlite3.Error:
            continue  # fragment of a concatenated statement
        names = {}
        for table, alias in ALIAS.findall(sql):
            names[table] = table
            if alias:
                names[alias] = table
        for step in plan:
            match = re.match(r'SCAN (\w+)$', step)
            if match and names.get(match[1], match[1]) in GROWING:
                key = f'{file}:{" ".join(sql.split())[:120]}'
                if key not in baseline:
                    hits.append(f'{file}:{node.lineno}: full scan of {names.get(match[1], match[1])}: {" ".join(sql.split())[:120]}')
print('\n'.join(hits))
sys.exit(1 if hits else 0)
```

在 `d0d587c` 上运行输出 25 行（退出码 1）；加上 P21、P26 的索引后，协作模块里的命中只剩附件配额的 `SUM`（P23 改成按用户统计后消失）和几处一次性迁移语句。

### A.7 P27 搜索误命中

```python
import json, sqlite3
def canonical(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def like(q): return '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
db = sqlite3.connect(':memory:'); db.execute('CREATE TABLE m(id, body)')
db.execute('INSERT INTO m VALUES (1,?)', (canonical({'body_text': '部署 "v2" 到\n生产', 'mentions': [],
    'notifications': [{'state': 'queued', 'slot_id': 'abc'}], 'request_digest': 'deadbeef'}),))
db.execute('INSERT INTO m VALUES (2,?)', (canonical({'body_text': '午饭吃什么', 'mentions': [], 'request_digest': 'cafe'}),))
for q in ['"v2"', '到\n生产', 'mentions', 'queued', 'deadbeef']:
    old = [r[0] for r in db.execute("SELECT id FROM m WHERE body LIKE ? ESCAPE '\\'", (like(q),))]
    new = [r[0] for r in db.execute("SELECT id FROM m WHERE json_extract(body,'$.body_text') LIKE ? ESCAPE '\\'", (like(q),))]
    print(f'{q!r:14} current={old}  fixed={new}')
# '"v2"'  current=[]  fixed=[1]      '到\n生产' current=[] fixed=[1]
# 'mentions' current=[1, 2] fixed=[]  'queued' current=[1] fixed=[]  'deadbeef' current=[1] fixed=[]
```

---

## 附录 B：静态分析结果摘要（ruff，910 条）

| 规则 | 数量 | 结论 |
| --- | --- | --- |
| S101 assert | 196 | 运行时校验里的 assert 在 `python -O` 下会消失；抽查均位于测试辅助或不变量检查，可接受 |
| S608 SQL 字符串拼接 | 161 | 抽查均为固定列名或 `?` 占位符个数拼接，无外部输入 |
| BLE001 宽泛 except | 104 | 多数为「诊断写入失败不影响主流程」的有意设计，且有计数器 |
| S603/S607 子进程 | 121 | 均为参数列表形式，无 shell |
| B023 闭包捕获循环变量 | 45 | 核对后均为立即执行的 lambda（`store.run(lambda: ...)`），误报 |
| F841 未使用变量 | 28 | 见 P11 |
| S314 XML 解析 | 14 | 解析对象为本机 plist / Windows 服务 XML，不是不可信输入 |
| S506 `yaml.load` | 1 | `MetadataLoader` 继承 `SafeLoader` 并限制别名、深度与节点数，安全 |
| S305 ECB 模式 | 2 | `weixin_protocol.py` 的媒体加解密，由微信协议规定，非本项目可选 |
| 其余（B904、B905、PLW1510、S108、S310 …） | 约 70 | 风格或低风险，未列入问题清单 |

前端：全部 JS 通过 `node --check`；`innerHTML` 插值均经过 `E()` / `esc()` / `html()` 转义，未发现可利用的 XSS；`EventSource` 与定时器在页面切换时有对应的关闭逻辑。
