# CodePier 文档索引

| 任务 | 当前文档 |
| --- | --- |
| 升级、备份与恢复 | [面板更新与备份恢复](PANEL_UPDATE.md) |
| 安装及维护执行节点 | [Agent 安装](AGENT_INSTALL.md)、[Agent 生命周期](AGENT_LIFECYCLE.md) |
| 接入 MCP、工具参数和授权 | [ChatGPT 接入](CHATGPT.md)、[核心工具](CORE_TOOLS.md)、[授权配置](ACCESS_PROFILES.md) |
| 调度、并发与资源诊断 | [自适应调度器](ADAPTIVE_SCHEDULER.md)、[并发验收](CONCURRENCY_ACCEPTANCE.md) |
| 协作、渠道与恢复 | [协作](COLLABORATION.md)、[协作恢复](COLLABORATION_RECOVERY.md) |
| 开发与验证 | [开发](DEVELOPMENT.md)、[架构](ARCHITECTURE.md) |
| 2026-11-18 优化审计 | [审计方案](FIRST_PRINCIPLES_OPTIMIZATION_2026-11-18.md)、[实施状态](FIRST_PRINCIPLES_OPTIMIZATION_2026-11-18_STATUS.md) |

源码引用采用相对路径和符号。例如 `hub/api/system.py::make_system_router` 提供健康探针，`hub/backup.py::restore_backup` 验证并恢复 Hub 数据，`hub/mcp.py::presentation_options` 解析协议呈现参数。仓库检查会验证此格式的 Python 路径和符号。带日期的审计、验收和设计记录保留其审阅基线，作为历史证据使用。
