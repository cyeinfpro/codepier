# 队列争用优化实施与运维

本实现把资源竞争、进程执行、数据库查询及回执可见性分开处理。默认仍为
`scheduler.resource_semantics = "strict"`。代码更新本身不改变生产节点配置，
不执行历史数据归档、VACUUM 或部署。生产收益必须通过同类工作负载的滚动
24 小时报告验证，合成测试不能替代该验证。

## 文件与进程的资源语义

Agent 宣告 `resource_semantics_protocol=2` 后才支持新参数。旧节点收到普通
调用时，Hub 删除新增的空默认字段，保留旧幂等指纹；非默认新能力在投递准备
和发送前都检查，旧节点明确拒绝，不静默降级。

- `strict`：保留原有文件/进程路径冲突及冲突 FIFO。
- `relaxed`：文件读取不等待进程写；文件写不等待进程普通读。
- `read_stable`：进程读取期间仍冻结覆盖路径上的文件写。
- 文件写之间、文件读与文件写、进程之间以及 service 资源保持互斥规则。
- 读取经历并发进程写时，结果带 `concurrency.mode=live`。Hub 在每次返回时
  重新过滤可见的写者；不可见操作只显示 restricted_or_unavailable。
- SHA、备份、原子替换、撤权、映射复核及操作恢复语义保持不变。

搜索、符号、历史查询只对请求 path 声明读，缺省才使用根。检查点读取源码
时申请稳定读。工作树创建另外协调 Git 元数据和新目录，不能把修改 Git
元数据误当成纯读取。

有界文件操作只在与所有运行中持有者不冲突、且每个冲突等待者仍受运行至少
5 秒的持有者阻挡时越过。原持有者释放后不再接受新的越过，已放行的文件
操作完成后原等待者恢复先后顺序。进程不越过；`options.order="strict"`
也关闭越过。联合资源/通道准入和快速准入使用相同判定，取消清理依赖边。

`read/write/edit` 的 file/batch 调用和 `exec` 支持
`options.wait_policy="fail_fast"`：初次等待窗口后仍存在真实资源阻塞时，
以 RESOURCE_BUSY 终止原操作，不执行文件或命令副作用。默认 queue 仍排队。
读取/写入默认初始窗口 1 秒；exec 使用 yield_seconds，0 时保留一个最小检查
窗口。等待通道额度本身不是 RESOURCE_BUSY。

## 命令与产物

本机 exec 在权限检查和准入之后创建：

```text
<workspace>/.codepier/runs/<operation_id>/
```

命令环境中的 `CODEPIER_RUN_DIR` 由 Agent 决定，不能由调用方覆盖。Git
info/exclude 忽略该目录，不改跟踪文件。回执包含相对 `run_directory`。
结束标记用于 7 天 TTL 清理；运行中目录不会按时间误删。面板提供复制相对
路径及原操作输出入口，不开放其他 .codepier 内部路径。目录内容可通过已授权
的本机命令或本机文件系统检查；不会自动注册、上传或分享产物。

推荐测试声明源码子目录 read，日志、报告、临时数据库写入独立运行目录。
独立运行目录彼此不重叠，但仍被宽泛的 `.` read/write 声明覆盖；不能为追求
并行而把真实路径冲突隐藏在另一个命名空间。根写回执提供缩小声明的提示。
VPS 命令保留原有远端环境，尚不注入本地 CODEPIER_RUN_DIR，应使用远端独立
目录。等待 CI 应使用短查询而不是长时间 watch。

## 快照执行

`exec(isolation="snapshot", snapshot_artifacts=[...])`：

1. 复核原项目执行许可、运行时、cwd 和资源范围。
2. 克隆阶段短暂持有源码稳定读和执行容量。
3. macOS 使用 clonefile，Linux 使用 reflink=always。不支持时返回
   SNAPSHOT_UNSUPPORTED，绝不退回完整复制。
4. 在 Agent 状态目录内的专有副本执行，不持续锁主源码。仍保留外部 service
   声明及本机运行目录声明。
5. 命令结束后把指定相对产物复制到运行目录；成功删除副本，失败/取消的副本
   保留至少 1 小时。清理前等待子进程停止，克隆线程取消时也等待真实完成。

首版只支持 `project_clean_v1` 的本地 command。native_compat、container、
配置 task、VPS 以及 snapshot+fail_fast 明确拒绝。原项目权限、映射与运行时
在启动前再次复核，不因副本而拓宽权限或绕过原有执行隔离要求。

快照排除 .git 和 .codepier。依赖 Git 的门禁使用已注册 worktree。外部/绝对
符号链接、editable 安装、可执行 .pth、指向原源码的解释器入口等会被保守
拒绝；原树相关环境路径会映射或拒绝。快照是源码并行机制，不是 OS sandbox，
命令的显式绝对路径不能被描述为自动隔离。

macOS 实际 clonefile 独立性已验证。Linux 云端使用真实 GNU cp 9.7 对合成
文件执行 reflink=always，当前文件系统返回 Operation not supported；原模块
正确拒绝并清理未完成副本，没有退回完整复制。支持 reflink 的 Linux 文件
系统上的成功独立性，以及大型 node_modules/venv 克隆时延仍需在目标节点
验收，不能以失败路径或模拟测试冒充成功实测。

## Hub 数据路径

- 2–4 个 mode=ro + query_only 连接（默认 3），写入仍通过单写线程。
- 每次只读事务获取新快照；结合 session revision/data_version 失效。
- 认证查询、状态/等待、授权结果和面板列表使用只读路径。文件导入的授权查询
  只核验已有完成证据，不在 reader 内恢复写入状态。角色拒绝审计从最终读取
  阶段收集，再交原单写线程持久化；失效重试不重复记审计。取消时等待实际
  查询与必要审计结束，不把仍运行的数据库线程提前归还。
- 投递锁按设备划分，投递热路径只读必需列。
- 发送前完整授权复核保留。设计中“跨 await 复用鉴权结论”未采用，避免削弱
  现有撤权与映射安全边界。
- 多 ID wait 在任一授权操作进入终态时返回；进度变化不会被误当成完成。
  默认 Hub 初始等待从 8 秒统一为 10 秒，没有增加客户端超时未验证的长轮询。

## 回执、面板与诊断

pending 回执的 `waiting` 包含原因、通道、排队年龄、可见阻塞操作与运行
时间。Hub 待投递、Agent 等资源、运行中分栏展示；取消入口沿用当前权限。
live 内容有标记，节点设置显示配置值与 Agent 实际生效值，仍使用 revision
确认流程。

实例管理员诊断报告统计滚动 24 小时等待比例、按原因等待时长、车队占比、
根阻塞者和阈值：等待超过 60 秒、项目等待超过 5 个、持有者阻挡超过 5 个。
面板同时显示数据库 worker 队列、阶段与锁等待指标。离线只读报告：

```sh
python -m scripts.queue_contention_report --database /path/to/hub.sqlite3 --top 10
```

报告是诊断信号，不自动取消任务或修改并发额度。性能 compose overlay 提供
1 GiB / 1.5 CPU 预算，继续沿用托管更新与回退的资源保留流程。应用到真实
主机须先核对共用服务预算并走授权部署流程。

## 历史归档及回滚

详见 [Hub 输出存储](HUB_OUTPUT_STORAGE.md)。终态超过 30 天且符合保留约束
的 result/output 可存入压缩加密归档表，读取透明恢复。密钥轮换、缺失密钥
保护、备份和还原均覆盖归档。离线维护要求 Hub 已停止，持有 Hub/密钥锁，
默认 dry-run；apply 前建立完整匹配数据库/密钥备份：

```sh
python -m scripts.archive_operation_payloads --data /path/to/data
python -m scripts.archive_operation_payloads --data /path/to/data --apply
python -m scripts.archive_operation_payloads --data /path/to/data --restore --apply
```

每次处理有界批次，需检查返回 operations 再安排后续批次。不自动 VACUUM，
不在本次开发中迁移真实数据。降级旧版本前先恢复 inline payload 并核验，
或恢复匹配的完整备份。

资源语义回滚只需把节点设置改回 strict；不取消已运行操作，等待队列按更
严格规则重新计算。快照是显式参数，不使用便不生效。归档回滚独立于队列
设置，不可把切回 strict 当成数据库降级步骤。

## 验证边界

新增回归覆盖冲突矩阵、短操作越过/原写者无饥饿、strict 顺序、定时重新准入、
联合准入取消、live 到达顺序、fail_fast 无副作用、批量读取默认参数、快照
克隆及进程取消、撤权/运行时变更、旧 Agent 投递、可见性过滤、只读连接池、
多 ID 等待、归档/还原及密钥轮换。面板另做 Chromium/WebKit 验收。

上线后才可判断实际 24 小时等待比例、排队 p99 和锁等待时长是否达到设计
目标。没有上线及真实对照数据时，不宣称“减少 60%/80%”已经实现。
