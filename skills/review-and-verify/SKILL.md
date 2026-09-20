---
name: review-and-verify
description: 检查 CodePier 的 Hub、Agent、面板与协议改动，记录真实执行证据并回归验证。
---

这是项目检查清单，不授予权限，也不会自动执行命令。

先解析项目并确认 execution_info。用 project_context 取得入口索引，再按需读取文件；索引不代表全仓扫描。查询 workflows_list，已有进行中任务则先读取目标、版本、断点与证据，不重复创建。

修改前确认现有改动。此目录可能不是 Git 仓库；没有 Git 时建立源码检查点，不声称已提交。保留并发修改，不覆盖本机配置、密钥或已有用户代码。

本项目重点验证：操作幂等与断网续跑、授权和项目映射边界、Hub/Agent 协议、SQLite 事务、文件路径与备份、真实退出码、浏览器旧响应不覆盖新会话。工作流保存进度不等于命令调度器；取消工作流不停止本机进程。

执行 `.venv/bin/python -m pytest -q`。新增功能先跑对应测试，再跑全量。前端还需 `node --check web/app.js` 和 `node --check web/workflows.js`，以及 tests/test_agentdock_integration.py 中的真实 Chromium 场景。不要把退出码非零说成网络错误；保存原操作号并读取输出。

用 workflows_update 记录观察结论和本轮 operation_id。标记步骤完成前核对实际结果与验收条件；全部必要步骤处理后，再写最终验收摘要。报告真实修改、执行结果和未验证范围，不用旧版本测试或部署记录代替本轮证明。
