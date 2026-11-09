# 文档导航

按任务找入口；这里的源码说明不代表目标节点已经升级或某个宿主已经通过真实验收。

## 安装与日常使用

- [完整入门](START-HERE.md)：部署 Hub、接入电脑、映射项目、连接客户端
- [Agent 安装与升级](AGENT_INSTALL.md)
- [面板目录授权与 SMB 文件兼容](AGENT_PATHS_AND_SMB.md)：路径管理、配置冲突、授权生效与网络文件系统恢复
- [连接 ChatGPT 与其他 MCP 客户端](CHATGPT.md)
- [连接改进验收](CONNECTION_ACCEPTANCE.md)：批读、连接证据、刷新提示、恢复与 Tunnel 的已验收边界
- [MCP 工具参考](CORE_TOOLS.md)：定向项目发现、最小上下文、动作诊断与原操作恢复
- [项目知识地图](PROJECT_KNOWLEDGE.md)：两个原生接口、源码 SHA 刷新、只读查询与自动参考
- [项目运行时](PROJECT_RUNTIMES.md)：运行模式切换与生效核对、默认清洁环境、项目任务、结果恢复及隔离边界
- [CLI 会话](CLI_SESSIONS.md)：Pi、Codex 和 Claude Code
- [浏览器集成](INTEGRATIONS-20260917.md)与[桌面控制](COMPUTER_USE.md)
- [统一设置中心](SETTINGS_CENTER.md)：个人、实例、项目和节点的设置入口
- [自适应并发与队列](ADAPTIVE_SCHEDULER.md)：并发上下限、资源压力、项目公平和生效回执
- [并发优化与容量验证](CONCURRENCY_OPTIMIZATION.md)：队列定位、发布能力、资源预算及可复现验收
- [Hub 输出与数据库观测](HUB_OUTPUT_STORAGE.md)：热表、历史兼容、事务保证和降级条件
- [文件入站](FILE_IMPORT_INGRESS.md)：限制、权限、客户端兼容证据、升级顺序
- [Hub 入站设置](hub-file-import-settings.md)：预览、确认、保存、继承与关闭
- [Token 与费用估算](mcp-token-usage.md)：MCP 工具文本、95% 输入缓存假设、参考模型、价格分项和真实用量边界
- [常见问题](FAQ.md)

## 权限、协作与维护

- [房间协作中心](COLLABORATION_CENTER.md)：建房、成员接入、项目范围、微信向导和原工作继续
- [协作中心维护与恢复](COLLABORATION_CENTER_OPERATIONS.md)：接入诊断、迁移、幂等回执与数据库恢复
- [协作中心全面重构设计](COLLABORATION_CENTER_REBUILD_DESIGN_2026-11-02.md)：房间、项目、dot 接入、微信、自主推进、迁移与验收

- [安全指南](../SECURITY.md)
- [身份与 OIDC](MULTIUSER_OIDC.md)
- [访问配置](ACCESS_PROFILES.md)与[动态角色](DYNAMIC_ROLES.md)
- [外部 MCP 网关](MCP_GATEWAY.md)与[并发隔离](GATEWAY_CONCURRENCY.md)
- [模型/CLI 与桌面能力策略](EXECUTION_CAPABILITIES.md)
- [原操作完成提示](OPERATION_EVENTS.md)：精确订阅、事务 outbox 与宿主消费边界
- [MCP Events 与多 Dot](MCP_EVENTS_MULTI_DOT.md)：v2 稳定收件箱、消费会话、团队交接、恢复、现场验收及离线工具
- [协作室](COLLABORATION_CHATROOM.md)与[统一工作区交互](COLLABORATION_WORKSPACE_UX_2026-10-11.md)
- [协作中心流畅交互与动效](COLLABORATION_FLUID_UX_2026-10-12.md)：增量呈现、输入热路径、状态一致性、浅深色与减少动态效果
- [有界协作恢复与通道边界](COLLABORATION_RECOVERY.md)
- [Telegram 与微信协作室同步](TELEGRAM_ROOM_SYNC.md)：本人私聊、独立投递与逐文件批准
- [面板更新与备份](PANEL_UPDATE.md)
- [长任务与回执恢复](LONG_OPERATIONS.md)
- [操作历史隐私与保留边界](OPERATION_HISTORY_PRIVACY.md)
- [VPS 连接](VPS.md)

## 开发与发布

- [开发和验证](DEVELOPMENT.md)
- [依赖输入与安装锁](DEPENDENCIES.md)
- [发布流程](RELEASING.md)
- [并发验收](CONCURRENCY_ACCEPTANCE.md)与[基准运行方法](PERFORMANCE_BENCHMARKS.md)
- [多 Hub 扩展门槛](HORIZONTAL_SCALING.md)
- [贡献说明](../CONTRIBUTING.md)
- [版本记录](../CHANGELOG.md)

仓库根部保留 README、许可证、安全/贡献入口，以及安装、构建和旧更新器需要的固定文件。Hub、Agent、网页、部署和测试分别在自己的目录中；依赖开发输入集中在 requirements/。不要把运行配置、凭据、数据库或上传资料放进源码树。

委托缺少 ID、旧工具目录与唤醒恢复见[委托接入与恢复](DELEGATION_CONSUMER_RECOVERY.md)。

- [微信本人任务与长期连接](WEIXIN_OWNER_TASKS.md)
