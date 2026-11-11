---
name: hub-agent-upgrade
description: 在用户授权发布后，协调 CodePier Hub 与 Agent 升级，保护当前远程控制通道和持久化状态。
---

仅在用户明确授权部署时使用；本清单不自动运行命令。

区分源码、已发布包、运行中的 Hub/Agent 版本及客户端工具目录。先用 query(action="project", project=...) 核对项目、权限、现有改动和部署文档，再检查真实服务管理方式。保留并发工作，不猜测 SSH 目标或输出密钥。

按 docs/PANEL_UPDATE.md、docs/AGENT_LIFECYCLE.md 和 docs/RELEASING.md 的当前协议执行。使用 SQLite 一致性备份；一并保存匹配的 master.keys.json、仍存在的 master.key、轮换日志及完整状态目录。Agent 需保存配置和状态。源码 checkpoint 不替代数据库备份。根据 shared/hub_data_contract.py 核对实际兼容契约；回滚恢复匹配代码、数据库与密钥，不伪改 schema/version。

先完成与改动相关的回归、依赖锁、生成物和发布包检查。全量或真实平台验收按发布需求运行，不用旧证据代替本轮结果。先验证可恢复的更新路径，再更新当前控制通道；不要在将被关闭的进程内等待自己重启。

长命令只提交一次，使用 act(action="exec", ...) 的 operation_id 经 query(action="job", ...) 续查。网络中断不表示命令未执行。升级后验证 health、initialize、当前 tools/list、query 项目读取、获准的 act 操作、原操作恢复及实际 Hub/Agent 版本。默认目录按 query / act / get_profile 核对，并检查启用的兼容 profile；不固定工具数量。

只在真实发布或部署证据成立后更新对应记录；本地 prepared 源码、Git 标签、GitHub Release 和线上部署分别记录。
