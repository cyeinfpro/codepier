# 队列优化与容量验证

本指南对应 v1.31.0。源码、同提交 CI、Release、镜像及生产运行版本分别核对；发布不证明已有节点完成升级。

## 从等待阶段定位瓶颈

一次调用可能先后等待 Hub 持久准入、投递、文件/服务资源和 Agent 执行额度。只有最终成功完成的操作计入成功吞吐。128 个待处理操作不等于 128 个命令正在运行。

| 观察 | 优先核对 |
| --- | --- |
| waiting_resource | 声明的路径/服务是否重叠，是否有长命令或外部 CI 等待持有根目录 |
| waiting_worker / project_limit | 本项目额度与其他项目保留容量 |
| waiting_worker / lane_capacity | execution、read、remote 对应通道的实际 capacity |
| resource_pressure | 新鲜 OS 内存、CPU、I/O 采样及恢复状态 |
| DB worker queue_ms 高 | 数据库阶段排队；进一步检查 phase_ms、lock 和 transaction 时间 |
| 原回执已终态、HTTP 仍慢 | 返回体、统计计算、传输及客户端等待，不再归因于任务执行队列 |

`process(trace)` 保留当前阶段的观察时间和等待年龄。Agent 的 elapsed_ms 属于同一处理尝试的单调时间，不能与 Hub 墙钟相减。阻塞操作只显示调用方当前有权查看的记录；`restricted_or_unavailable` 不能解释为无锁或死锁。

## 调度实现

资源队列为每个等待项记录冲突依赖，释放时只更新相关依赖。联合调度器在同一次不可中断决策内授予整组资源和执行额度，再唤醒所属等待者。它保留冲突顺序、共享读取、不同路径独立推进、项目公平、远程目标额度和压力收缩。取消、排队超时、异常及重复取消都必须清理等待项与已授予资源。

最小项目索引是有界观察：逐个核验授权路径、规则文件和摘要，返回观察时间。它不取得根目录锁，也不承诺跨文件原子快照。显式 documents 预览、capture_baseline 以及写入仍保留各自一致性和互斥约束。

文本读取仍使用单操作独立进程。图片处理和桌面模型在实际图片/桌面边界才导入，避免每次 UTF-8 读取的无关冷启动成本。没有跨操作继承文件引擎、授权、项目映射或读取结果的常驻池。

## Hub 热点

统计先在数据库阶段形成当前授权快照，再交由最多两个计算线程解析与汇总。同一统计范围共享计算，最多八个范围在途；排队项不提前持有整份 10000 行快照。统计数据允许明确标注的 30 秒刷新窗口，授权与可见性变化不等待该窗口。缓存中不保留凭据、Principal 或项目根路径。

默认 `/api/overview` 保留原完整响应；`include_token_usage=false` 只读取即时概览。面板使用此轻量入口并独立请求 `/api/token-usage`，晚到或失败的统计不会替换当前页面。具体估算口径见 [Token 文档](mcp-token-usage.md)。

活动尾部、终态冻结、索引和锁观测见 [Hub 输出存储](HUB_OUTPUT_STORAGE.md)。阶段回放每批只提交一次，已保存的重复序号不再写入。业务审计、持久幂等记录和完成 outbox 不因此丢弃。

## 声明资源与运行 CI

测试命令按真实源目录声明 read，按独立测试产物目录声明 write；如果测试会改源码，必须声明对应写资源。多个 worker 使用不同临时数据库、缓存和报告目录。未声明资源不是隔离证明，不能借此绕过互斥。

等待外部 GitHub Actions 时，使用短时只读状态查询并查询原操作编号。等待 CI 不需要持续持有本地源码写锁。必须记录精确提交与原 run，不因为状态暂不可得就再次提交工作流或改变幂等键。

## 可选的容器预算

在确认主机有足够余量后，先保留实际 `COMPOSE_FILE` 或当前使用的完整 Compose 文件序列，在末尾追加性能覆盖文件。不能用基础 `compose.yml` 替换既有 HTTPS 覆盖或 `.codepier-updater/layouts/...` 独立状态卷布局；否则可能遗漏状态卷挂载、改变 `HUB_STATE_DIR` 或启动错误的历史数据库。

下面的配置检查仅适用于当前配置恰为 `compose.yml`、尚未分离状态卷且没有其他覆盖文件的新部署或未托管部署：

```sh
docker compose -f compose.yml -f deploy/compose.performance.yml config
```

覆盖文件默认给 Hub 2 CPU 上限、1 GiB 内存上限、512 MiB reservation、512 pids 和有界日志保留。可通过 `.env.example` 中的 `CODEPIER_HUB_*` 与 `CODEPIER_LOG_*` 设置调整。实际应用前，核对合并结果中的镜像、服务、全部卷、`HUB_STATE_DIR` 与资源限制。

已由更新服务托管的部署应将此文件视为预算参考，按既有配置变更流程把资源设置合入当前已登记的完整布局，再核对 `current.json` 实际记录。仅修改 `.env`、一次性添加 `-f`，或重装更新服务，都不保证新增覆盖文件被登记并在后续更新中保留。更新器的 `DEPLOYMENT_CHANGED` 检查不能绕过，参见 [面板更新](PANEL_UPDATE.md)。普通查询和源码发布不会应用此预算。

比较升级前后的 cgroup memory.current、memory.events、CPU throttle、进程 RSS、DB worker 排队与磁盘余量。memory.current 包含缓存，不能全部当作 Python 堆；历史 memory.events 计数需要看增量。资源上限应由真实稳定负载验证，不能仅因主机空闲就把所有并发提高到 64。

## 验证和容量结论

用同一脚本、解释器、依赖和负载，分别运行明确的旧/新源码。独立读取、读写混合、长短任务混合分别测量 8、16、32、64、128 并发，并保留拒绝、失败、断线恢复与全部原始样本。工具与命令见 [基准指南](PERFORMANCE_BENCHMARKS.md)。冷启动与稳定运行分开报告；不能在旧版使用冷启动、新版预热后比较。

发布需要联合准入/取消、旧 Agent 输出、完整旧库迁移、撤权、跨用户统计、关闭清理、前端延迟响应等回归，并遵守完整 Linux/macOS 六片、Windows 核心、OIDC、公开归档及镜像门禁。目标值和实测值分开记录；未测得的千级并发或吞吐倍数不写成已支持能力。

## 多 Hub 边界

此版本继续单权威 Hub、多 Agent。多台 Agent 可以并行执行不同已授权项目；同一远程服务仍受资源声明与目标额度约束。

多 Hub active-active 尚未实现。除共享任务、权限、幂等、审计和 outbox 外，还需迁移 NativeService/ArtifactService 的回包所有权、Gateway 有状态会话、设备连接、取消和后台领取。必须通过真实双实例的失主接管、旧 owner 恢复、数据库故障、网络分区、撤权和重复回调验收；不得简单取消 InstanceLock 或让多个 Uvicorn worker 共写现有状态。详细门槛见 [多 Hub 扩展](HORIZONTAL_SCALING.md)。
