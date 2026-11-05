# 队列面板实现与定向验证（2026-11-05）

## 范围与基准

- 队列工作树：`83693af9cc514623891c3a620b56ad75`，基于 `93a56517768bb274f7481a53463ed4ee897336d6`。
- 实施 `QUEUE_CONTENTION_DESIGN_2026-11-05.md` 的面板、可观测性入口、资源语义设置和可选资源预算要求。
- 与 Hub 数据路径、归档报告、Agent 调度执行者共享该工作树，未覆盖其文件；本报告只记录面板分工。
- 之前 UI 设计系统提交 `00902463cc954ffa157c2608393c3dc6848b6f2d` 保留在独立工作树，本次未提前合入。
- 未推送、未触发 CI、未部署生产，未读取或发送真实用户内容。

## 已实现

1. 调用日志上方增加三列实时队列：Hub 待投递、Agent 等资源、运行中。展示等待原因、通道、等待时间、已授权阻塞者及其摘要、状态与耗时。受限阻塞者只显示不可用提示。
2. 新增只读 `GET /api/operations/queue`。先按空间、私有归属和项目粗筛，再对每项调用完整 `operation_row(status_only=True)` 授权；不读取结果、输出或解密请求。每次最多核验 1,000 个候选并显示 100 个可见项，超出可见展示上限给出提示。
3. 取消入口复用正式 `operations_cancel`，按工具/已接收次数/执行权限控制可用性。确认告知副作用不会回滚，提交后只称“请求取消”，由持久回执确认结果。防止重复提交、导航后旧确认执行、旧取消收尾干扰新页面。
4. 新增实例管理员专用 `GET /api/queue-diagnostics`，通过只读 worker 和一致读事务聚合 24 小时等待、车队比率、根阻塞者及阈值告警。显示 Store worker 排队、phase、lock 等待分位；不向普通空间管理员开放实例指标。权限撤销及诊断晚到不会恢复旧视图。
5. 操作详情与调用详情展示 live 读取标识及已授权并行写入者。明确不是一致快照，SHA 对应本次读取的字节。
6. 展示 `.codepier/runs/<operation_id>` 相对运行目录及快照产物名，提供复制位置。路径严格限定操作目录形状，不将 `.codepier` 保护根开放给通用文件浏览，不自动下载或分享。
7. 节点设置增加继承/strict/relaxed 选择；保存修订与 Agent 实际报告分开呈现。旧协议节点明确仍为 strict，新节点未报告时不声称已生效。保留草稿、CAS、丢失回执恢复与节点确认流程。
8. 可选 `deploy/compose.performance.yml` 默认 CPU 改为 1.5，保留 1 GiB 内存上限。合成 Docker 边界验证注册的预算在 prepare、commit、rollback 的受管布局中保留。未注册或启用真实部署预算。
9. 按现有 token 样式实现，桌面三列、窄屏单列，触摸按钮最小 44 px；轮询不替换当前聚焦按钮或选中的证据，随调用日志暂停或离开而停止。

## 定向验证

锁定环境为 requirements-dev.txt 哈希校验安装的 Python 3.13 / Playwright 1.64.1；Chromium 1248 与 WebKit 2370。Mac Chromium 使用仓库明确的完整 Chromium 无头启动策略。测试使用临时 Hub、Agent、SQLite 和合成内容，不连接生产。

最终相关定向验证共 **110 个不同案例通过**：

- `tests/test_queue_panel.py`：18，通过权限/私有归属/跨空间隔离、实例诊断门禁、无冷数据读取、完整来源授权、阻塞者、展示上限、取消能力、实际资源语义。
- `tests/test_scheduler_settings.py` 与 `tests/test_panel_update_runtime.py`：22，通过现有设置流程及新增预算持久化断言。
- `tests/test_call_log_browser.py`：32，Chromium/WebKit 既有调用日志回归通过。
- `tests/test_scheduler_settings_browser.py`：24，真实 Hub/Agent，双引擎桌面/390px 移动视口，包含 relaxed 保存并生效、保持草稿后切回 strict。
- `tests/test_queue_panel_browser.py`：最终 14，通过三阶段、阻塞者、XSS、窄屏、运行目录安全入口、重复取消、导航、聚焦稳定、权限撤销、诊断晚到和旧取消收尾。

新增 API 夹具最初两处违反数据库外键/跨空间约束，已改为合法合成身份/空间；等待状态也按真实协议改为 queued。修正后 18 项全部通过，没有放宽产品权限或断言。

定向 Ruff、Prettier、`git diff --check`、`build_web_assets.py --check` 通过。最终资源清单在 JS/CSS 收尾后重建。源码及测试于 2026-11-05 02:06:21 UTC 冻结。该证据不替代协调者随后进行的全量验证。

日志在 `.work/queue-panel-api3.log`、`.work/queue-panel-api.log`、`.work/queue-panel-browser.log`、`.work/queue-panel-browser-final.log`、`.work/queue-scheduler-browser.log`。
移动截图在 `.work/queue-panel-browser-final/test_three_stages_blockers_xss*/queue-mobile.png`；已检查 WebKit 截图，无横向溢出。Chromium 该机器全页截图有重复栅格片段，交互/布局断言均通过，不能将该截图当作完整页面视觉证明。

## 合并与交付注意

- 本分工改动：`hub/api/activity.py`、`hub/queue_panel.py`、`hub/scheduler_settings.py`；`web/queue.js`、`web/queue.css`、`web/app.js`、`web/call-log.js`、`web/settings-scheduler.js`、`web/index.source.html` 及生成的 `web/index.html`/`web/assets-manifest.json`；Compose 预算；上述新增/扩展测试。
- 必须与 Hub 的 `queue_annotations`、`database_endpoint(read_only=True)`、`queue_contention_report`，以及 Agent/共享配置的 resource_semantics 协议一起集成。
- 与 UI 提交 00902463 合并时，`web/app.js` 新操作详情片段与旧确认框迁移分别保留；`index.source.html` 保留新队列资源和 UI 提交的 experience 样式移除。生成资源不要择一覆盖，应在合并源码后重建并复验。
- 新取消入口兼容 00902463 的 `CP.ui.confirmSheet({title,body,confirmLabel,danger})`；当前旧基线仍可使用浏览器原生确认。需在最终组合树再覆盖模态焦点/异步取消验收。
- 运行目录当前为安全复制/产物名入口，而非浏览受保护目录。真正下载或受限文件读取应另行设计最小授权接口，不能泛化开放保护根。
- 可选预算仅落配置及合成持久化证明；实际资源预算部署、生产启用和全量 CI 均由相应授权流程处理。

## 全量前端失败复核（03:26 UTC）

全量首轮的三项前端失败均为当前工作树缺少 Node 锁定依赖，未进入被测产品逻辑：

- `test_template_lowering_preserves_runtime_string_bytes`：导入 esbuild 时 ERR_MODULE_NOT_FOUND。
- SDK 卡片 1100/390 两项：host_bundle fixture 检测不到 `node_modules/esbuild/lib/main.js`，按设计要求安装锁定依赖。
- 在该工作树执行 `npm --prefix web/mcp-apps ci --ignore-scripts` 后，`test_generated_release_hygiene.py` 与 `test_mcp_apps_host.py` 合计 4 项通过（9.87 秒）。无需修改生产源码或测试断言。
- 原始全量失败日志保留；补验日志 `.work/queue-frontend-dependency-check.log`，原操作回执 `070b67c4419645b39d024f76085a1e74`。
- 此补验仅解除这三项环境阻塞，不代表其余全量失败或跳过已经解决。本机 Node 22.22.0，正式 CI 仍按仓库要求使用 Node 24。
