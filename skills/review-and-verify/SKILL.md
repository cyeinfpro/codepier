---
name: review-and-verify
description: 检查 CodePier 的 Hub、Agent、面板与协议改动，记录本轮执行证据并回归验证。
---

这是项目检查清单，不授予额外权限。

先用 query(action="project", project=...) 打开项目，读取 AGENTS.md、MEMORY.md 和相关子目录规则。通过 Git 状态核对分支、现有改动及并发工作；保留用户修改。用 query(action="workflow", ...) 了解已有进度，具体参数由 query(action="help", ...) 查询，不沿用旧工具名。

根据变更追踪真实调用链，重点检查幂等与断网恢复、授权、Hub/Agent 协议、SQLite 事务、文件边界、取消清理、真实退出码和浏览器旧响应覆盖新会话。先跑相关模块；仅在发布范围或风险需要时扩大到全量。

使用锁定的 Python 3.13 环境；宿主安装/更新脚本另跑 scripts/check_host_python.py，CI 还以 Python 3.9 执行导入检查。运行 Ruff、已配置的 mypy 及 scripts/check_dependencies.py。前端按 web/mcp-apps/package.json 构建和检查所有源文件，核对生成物，再运行相关 Chromium/WebKit 场景。参考 docs/VERIFICATION_TOOLS.md 选择平台验收，真实账号、模型和部署不属于普通回归。

通过 act(action="exec", ...) 执行，保存 operation_id；pending 时经 query(action="job", ...) 读取原任务，不重复提交。测试使用合成项目与临时数据库。仅在受保护的默认临时目录影响测试时，为该测试进程设置 TMPDIR=/tmp，不更改 Agent 全局保护。

完成后核对 git diff --check、生成物及本轮日志。用已建立的 workflow 记录证据；需要修改时使用 act(action="workflow", ...) 的当前接口。报告具体修复、测试结果、未验证的平台以及是否提交/发布/部署。
