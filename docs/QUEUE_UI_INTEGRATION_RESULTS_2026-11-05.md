# 队列与 UI 设计系统集成验证（2026-11-05）

## 目标与边界

在独立工作树、分支 `integration/queue-ui-20261105` 中整合队列与 UI 工作。基线为已核实远端 main 的 `acbfffe8ae1c90053c79ecb8919332c2c74a710b`；完成前再次查询远端仍为此提交。

不改变主工作区的两份未跟踪设计文档，不修改运行中的发布工作树或依赖环境，不推送 main，不触发或取消 CI，不部署或迁移生产数据。本次不是全量验收、正式发布 CI 或上线证明。

## 集成来源与冲突处理

- 队列 `9ef9f4c801dceb2dbb8a5d1012d97c1d8e930772` → 集成 `7794c99`。
- 队列补强 `f5ef09a3b02a82b9d3b33395a3f5a8a9d5d2abe7` → 集成 `207a3c5`。
- UI `00902463cc954ffa157c2608393c3dc6848b6f2d` → 集成 `bf11951`。
- `web/app.js` 自动合并后逐段核对：保留 UI 的操作详情上下文/代次守卫，以及队列 waiting、live、run 三类详情。
- `web/index.source.html` 保留 queue.css 与 queue.js 加载，同时保留 UI 移除 experience.css 的入口变更。
- 冲突只出现在生成的 index.html 和 assets-manifest.json，完全从合并后的源码重建，不手工拼接。
- 已核对 agent、hub、shared 相对队列最终提交没有额外差异，后端双方功能未被 UI 合并覆盖。

## 交叉验收发现并修复

1. 队列原生 confirm 后备路径使 UI 漂移护栏从 12 增至 13。集成后使用统一确认层；组件缺失时明确提示并阻止提交，不降级执行或放宽基线。
2. 打开确认层前禁用队列按钮，使关闭确认层时无法恢复键盘焦点。只在原页面/会话/空间/挂载仍有效，且焦点没有被其他控件接管时恢复原按钮。
3. 暂停实时更新时，取消请求完成后刷新生成的新按钮继承 pending 状态，一直禁用至下一次刷新。请求结算后先清除对应 pending，再刷新；仍保留请求进行期间的重复点击保护及旧挂载回调隔离。增加明确的“仅提交取消请求，等待执行回执”的提示，不把请求回执描述为操作已取消。

新增 14 条 Chromium/WebKit 真实共享确认层案例，覆盖浅/深色窄屏、Esc 关闭和焦点、单次提交、请求结算、页面/会话/空间/挂载变化、确认组件缺失时拒绝执行。原有桩驱动延迟/权限/重复操作覆盖仍保留。

## 实际验证

合成数据、临时 Hub/SQLite 和隔离浏览器。最终共 **13 个模块、219 个不同案例通过**，分三组串行运行：

- 队列浏览器与 UI 漂移护栏：36 passed。
  - test_queue_panel_browser.py（28，含新增 14）
  - test_ui_design_guard.py（8）
- 权限、诊断、设备调度、资源语义与归档：87 passed。
  - test_queue_panel.py
  - test_read_denial_audit.py
  - test_incoming_read_authorization.py
  - test_coordination_device_dispatch.py
  - test_queue_hub_optimizations.py
  - test_resource_semantics_v2.py
  - test_queue_contention_archive.py
- 共享浮层、真实页面布局、调度设置和审计入口：96 passed。
  - test_ui_layers.py
  - test_ui_design_layout.py
  - test_scheduler_settings_browser.py
  - test_audit_web.py

截图最初捕获到入场动画中间帧，之后增强测试等待实际 opacity=1，再执行相关 4 条案例，全部通过。已实际查看最终浅色 Chromium、深色 WebKit 的 390×844 可视区，弹窗文字、边界、按钮和遮罩正常。该 4 条是重复验证，不额外加入 219 的独立案例数。

前期失败被用于定位护栏、焦点和暂停刷新问题；测试辅助 API/表达式错误也在最终验收前修正。早期失败批次不作为通过证据。最终没有跳过、超时、测试失败或 teardown 错误。

检查通过：

- npm ci --ignore-scripts 使用项目锁文件安装独立 Node 依赖，审计 0 vulnerabilities。
- npm run format:check、完整前端构建、integration assets 构建、web assets 生成与 --check。
- 最终再次构建输出保持一致；所有 58 个入口资源哈希通过校验。
- Ruff 全仓正确性检查、项目既有 mypy 范围（3 个源文件）、git diff --check。
- 队列引用的 CSS token 均有定义；既有设计指标基线未放宽。
- scripts/check_release.py 的公开输入一致性/已知凭据模式检查通过。未打包或公开测试目录。

验证结果保留于该工作树的 `.work/integration-queue-ui/`，包括三个最终 JUnit 文件、截图复测 JUnit、公开输入报告与合成截图。该目录不提交、不公开。

## 未验证与限制

- Python 3.13.12、Playwright 1.64.1、匹配的官方 Chromium/WebKit；只读使用已有已验证环境和浏览器缓存，未修改它们。Node 为本机 22.22.0，不是项目推荐的 Node 24。
- 未重跑全仓/多平台回归、Windows/Linux、资源压力/10–30 分钟墙钟验收、正式 CI、真实账户、真实移动软键盘或生产部署。相关原分支验证仅为来源背景，不当作新集成提交的正式 CI 结果。
- 本任务未变更认证授权、凭据或生产状态，不进行真实付费服务调用。
- CodePier 审阅快照因 REVIEW_QUOTA 无法创建；以精确 Git 基线、独立工作树、差异审查与测试证明记录整合过程，不尝试删除旧快照或绕过配额。
