# CodePier 面板 UI/UX 布局深度排查 · 2026-11-10

> 工作区：MCP · `main`。本轮聚焦前端结构、排版与移动端布局，保留既有黑/黄/白视觉逻辑。
> **第一轮状态：已落地第一轮跨页根因修复，静态检查通过；不是“全部页面已视觉验收”。**

## 1. 审查范围与事实

- 在修改前通过 CodePier baseline 完整保存 `web/` 下 **81 份手写 JS/MJS/CSS/HTML**（`baseline_ref=5f7be8c45cd041d38639f8d37d0a1766`）。
- 源码扫描按目录、文件类型与关键 UI 声明覆盖全部 81 份文件；22 份首方 CSS 完整语法解析，前端 JS 执行 `node --check`，包含内嵌 App、浏览器扩展和共享模块。
- `web/app.js` 的导航对应 20 个路由：`native`、`overview`、`devices`、`projects`、`vps`、`workbench`、`workflows`、`collaboration`、`audit`、`connect`、`profiles`、`roles`、`mcp-gateway`、`diagnostics`、`integrations`、`artifacts`、`identity`、`members`、`identity-admin`、`settings`。
- 自动提取到约 95 处 modal / openPanel / confirmSheet 等前端调用点（含函数定义与调用），主要集中在 `app.js`、`collaboration-room.js`、`workflows.js`、`insights.js`。**这些调用点尚未逐个在浏览器里打开截图。**
- 对按 `index.source.html` 顺序加载的 CSS 查重，跨文件同选择器属性冲突只有少量，最严重的移动顶栏被功能样式全局覆盖问题已修复；其他检测到的登录页面双列→单列覆盖属于现有页面设计。
- 不将目录扫描、静态解析或代码修复等同于 20 路由全部视觉验收；动态验收限制见第 4 节。

## 2. 已落地修复

| 页面/组件 | 具体根因 | 改法 | 源文件 |
|---|---|---|---|
| 全部页面移动顶栏 | 更新模块中的 `.topbar` 移动媒体规则覆盖共享顶栏，导致跨路由换行与高度漂移 | 删除更新模块对全站 `.topbar` 的重定义，只收缩更新按钮自身并隐藏冗余状态标签 | `web/panel-update.css` |
| 设备信息卡片 | `grid-template-columns:1fr 1fr` 的隐含 min-content 宽度会被长信息撑开 | 用 `repeat(2,minmax(0,1fr))` 允许内容换行收缩 | `web/styles.css` |
| VPS 服务器信息 | 双列信息在长地址和 ID 场景有相同风险 | 使用可收缩双列网格 | `web/vps.css` |
| 设置工具栏与设置步骤卡 | 宽文本可挤压列宽；移动卡片第二列不可收缩 | 两列用 `minmax(0,1fr)`；移动卡片改为 `32px minmax(0,1fr)` | `web/settings.css` |
| CLI 会话（320px） | 固定宽度“返回面板”按钮挤压标题与右侧操作 | 超窄屏缩为 44px 纯图标按钮，补充清晰的 `aria-label`；弹出菜单标题列允许收缩 | `web/chat.css`、`web/chat-chrome.js` |
| 协作中心房间操作栏、权限编辑 | 移动按钮栏 `1fr` 列难收缩、权限“项目名称＋选择框”两列过窄 | 房间操作栏用 `minmax(0,1fr)`，≤460px 权限行改上下布局 | `web/collaboration-room.css` |
| 手机键盘打开时的会话/协作 | 布局视口与可视视口不同，输入区或底部弹层可能被软键盘遮挡 | 使用已存在的 `ui-keyboard-open` 与 `--ui-visual-viewport-bottom` 让聊天窗口、房间容器和移动协作底部弹层避开软键盘 | `web/chat.css`、`web/styles.css`、`web/collaboration-room.css` |
| 矮屏弹窗 | 非编辑类弹窗底部按钮区域会与正文挤占空间 | 矮屏下压缩标题高度、对按钮栏限制高度并单独滚动 | `web/styles.css` |
| 页面分区与协作标签 | 额外非规范字重 650/normal、裸写圆角导致设计 token 漂移 | 统一到 `--ui-weight-semibold`、`--ui-weight-regular`、`--ui-radius-md` | `web/workspace.css`、`web/collaboration-room.css` |
| 前端启动资源 | 已修改 CSS/JS 的哈希与已生成的资源清单不一致，真实测试 Hub 拒绝启动 | 运行 `scripts/build_web_assets.py` 重新生成 `web/index.html` 与 `web/assets-manifest.json`（后续所有改动完成后需再运行一次） | 两份生成文件 |

## 3. 验证结果（区分静态与真实 UI）

| 验证 | 结果 |
|---|---|
| 首方 CSS 语法解析 | **22 份通过** |
| `node --check` 检查前端 JS/MJS（包括安装依赖） | **303 份、0 语法失败** |
| 浏览器扩展 `node --test` | **29 项通过** |
| `tests/test_ui_design_guard.py` | **8 项通过**；改动后规范漂移计数恢复至阈值内 |
| 新增 `tests/test_ui_layout_contracts.py` | **7 项通过**（源代码结构级响应式回归） |
| `git diff --check` | 通过（范围为本轮前端修改文件） |
| 浏览器逐页视觉截图 | **未完成，见下方限制** |
| 实际部署与发布 | **未进行**；本轮修改先留在工作区以便与其他正在进行的提交协调 |

## 4. 动态验收阻断及遗留风险

1. 当前 CodePier 连接的原生 GUI 能力未启用或未授权；普通文件读写与命令执行允许，但不可使用连接器自动遍历真实 UI。
2. Playwright 模块可导入，但隔离运行环境中缺少对应 Chromium 可执行程序；尝试无副作用的只读合成 19 路由×5 视口布局探针时，在启动浏览器前就因找不到程序退出，因此**不能声称 320/390/768/1024/1440 五档已截图通过**。
3. 现有浏览器验收测试的 Hub+Agent fixture 在自动创建受保护目录的项目时收到 `409 PROTECTED_ROOT`；不改宽权限规则绕过这个拒绝，故未能完成端到端复测。
4. 因为无法逐个打开实际页面，仍需在允许的真实浏览器环境复测：20 主路由的空/加载/错误/编辑状态；所有 95 处潜在 modal/面板入口（须去重后逐项）；浅色/深色与不同系统字体；键盘升起、横竖屏切换、表格长路径、表单校验、焦点与 Escape、弹窗叠层与滚动恢复。
5. 代码仓库有其他会话同时修改协作中心及 CI/发布相关文件。保持已有改动，不执行 reset/stash；本轮**没有修改 CI/部署权限逻辑、没有提交或推送**。

## 5. 逐文件源码扫描矩阵

> 此表中的“源码扫描”指逐文件纳入自动检查/目录与关键布局声明扫描，**不代表浏览器里完成逐页面目视验收**。某些纯工具/生成模块无独立可视页面，但仍纳入对应模块的源代码检查。

| 文件 | UI 模块/覆盖点 | 本轮结果 |
|---|---|---|
| `web/browser-extension/background.js` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/background.test.mjs` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/cleanup.test.mjs` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/idle.html` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/page.js` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/popup.css` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/popup.html` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/popup.js` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/workspace.js` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/browser-extension/workspace.test.mjs` | 浏览器扩展/弹窗 | 源码扫描；待动态验收 |
| `web/core/actions.mjs` | 共享 UI/DOM/生命周期 | 源码扫描；待动态验收 |
| `web/core/bundle.js` | 共享 UI/DOM/生命周期 | 源码扫描；待动态验收 |
| `web/core/dom.mjs` | 共享 UI/DOM/生命周期 | 源码扫描；待动态验收 |
| `web/core/index.mjs` | 共享 UI/DOM/生命周期 | 源码扫描；待动态验收 |
| `web/core/lifecycle.mjs` | 共享 UI/DOM/生命周期 | 源码扫描；待动态验收 |
| `web/core/ui.mjs` | 共享 UI/DOM/生命周期 | 源码扫描；待动态验收 |
| `web/mcp-apps/app.css` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/mcp-apps/app.js` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/mcp-apps/build-core.mjs` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/mcp-apps/build-options.mjs` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/mcp-apps/build.mjs` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/mcp-apps/review.js` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/mcp-apps/ui.js` | ChatGPT 内嵌 App | 源码扫描；待动态验收 |
| `web/access-profiles.css` | 身份/授权/成员 | 源码扫描；待动态验收 |
| `web/access-profiles.js` | 身份/授权/成员 | 源码扫描；待动态验收 |
| `web/access.js` | 身份/授权/成员 | 源码扫描；待动态验收 |
| `web/agent-install.js` | 电脑接入与路径授权 | 源码扫描；待动态验收 |
| `web/agent-roots.css` | 电脑接入与路径授权 | 源码扫描；待动态验收 |
| `web/agent-roots.js` | 电脑接入与路径授权 | 源码扫描；待动态验收 |
| `web/app.js` | 全局导航与 20 路由 | 源码扫描；待动态验收 |
| `web/appearance.js` | 全站布局与设计规范 | 源码扫描；待动态验收 |
| `web/brand.js` | 全站布局与设计规范 | 源码扫描；待动态验收 |
| `web/call-log.css` | 操作审计 | 源码扫描；待动态验收 |
| `web/call-log.js` | 操作审计 | 源码扫描；待动态验收 |
| `web/chat-catalog.js` | CLI 会话与子面板 | 源码扫描；待动态验收 |
| `web/chat-chrome.js` | CLI 会话与子面板 | 已做定点修改＋静态检查 |
| `web/chat-history.js` | CLI 会话与子面板 | 源码扫描；待动态验收 |
| `web/chat-markdown.js` | CLI 会话与子面板 | 源码扫描；待动态验收 |
| `web/chat-panels.js` | CLI 会话与子面板 | 源码扫描；待动态验收 |
| `web/chat.css` | CLI 会话与子面板 | 已做定点修改＋静态检查 |
| `web/chat.js` | CLI 会话与子面板 | 源码扫描；待动态验收 |
| `web/collaboration-room-store.js` | 协作中心与底部面板 | 源码扫描；待动态验收 |
| `web/collaboration-room.css` | 协作中心与底部面板 | 已做定点修改＋静态检查 |
| `web/collaboration-room.js` | 协作中心与底部面板 | 源码扫描；待动态验收 |
| `web/computer.css` | 电脑远程操作/审批 | 源码扫描；待动态验收 |
| `web/computer.js` | 电脑远程操作/审批 | 源码扫描；待动态验收 |
| `web/connection-setup.js` | 其他 UI 支撑 | 源码扫描；待动态验收 |
| `web/experience.css` | 全站布局与设计规范 | 源码扫描；待动态验收 |
| `web/identity.css` | 身份/授权/成员 | 源码扫描；待动态验收 |
| `web/identity.js` | 身份/授权/成员 | 源码扫描；待动态验收 |
| `web/index.html` | HTML 启动与资源 | 源码扫描；待动态验收 |
| `web/index.source.html` | HTML 启动与资源 | 源码扫描；待动态验收 |
| `web/insights.js` | 诊断与洞察 | 源码扫描；待动态验收 |
| `web/integration-flow.css` | 开发工具/向导 | 源码扫描；待动态验收 |
| `web/integration-ui.js` | 开发工具/向导 | 源码扫描；待动态验收 |
| `web/integrations.css` | 开发工具/向导 | 源码扫描；待动态验收 |
| `web/integrations.js` | 开发工具/向导 | 源码扫描；待动态验收 |
| `web/mcp-gateway.css` | MCP 网关 | 源码扫描；待动态验收 |
| `web/mcp-gateway.js` | MCP 网关 | 源码扫描；待动态验收 |
| `web/native-hash.js` | 其他 UI 支撑 | 源码扫描；待动态验收 |
| `web/panel-update.css` | 版本更新入口与详情 | 已做定点修改＋静态检查 |
| `web/panel-update.js` | 版本更新入口与详情 | 源码扫描；待动态验收 |
| `web/project-runtimes.css` | 运行时/凭据表单 | 源码扫描；待动态验收 |
| `web/project-runtimes.js` | 运行时/凭据表单 | 源码扫描；待动态验收 |
| `web/queue.css` | 任务队列 | 源码扫描；待动态验收 |
| `web/queue.js` | 任务队列 | 源码扫描；待动态验收 |
| `web/roles.js` | 身份/授权/成员 | 源码扫描；待动态验收 |
| `web/settings-file-import.js` | 设置/配置表单 | 源码扫描；待动态验收 |
| `web/settings-scheduler.js` | 设置/配置表单 | 源码扫描；待动态验收 |
| `web/settings.css` | 设置/配置表单 | 已做定点修改＋静态检查 |
| `web/settings.js` | 设置/配置表单 | 源码扫描；待动态验收 |
| `web/skills.js` | 技能管理 | 源码扫描；待动态验收 |
| `web/styles.css` | 全站布局与设计规范 | 已做定点修改＋静态检查 |
| `web/token-usage.css` | 用量统计 | 源码扫描；待动态验收 |
| `web/token-usage.js` | 用量统计 | 源码扫描；待动态验收 |
| `web/tokens.css` | 全站布局与设计规范 | 源码扫描；待动态验收 |
| `web/ui.js` | 全站布局与设计规范 | 源码扫描；待动态验收 |
| `web/vps.css` | VPS 列表/连接检查 | 已做定点修改＋静态检查 |
| `web/vps.js` | VPS 列表/连接检查 | 源码扫描；待动态验收 |
| `web/workflows.js` | 开发任务/进度 | 源码扫描；待动态验收 |
| `web/workspace.css` | 全站布局与设计规范 | 已做定点修改＋静态检查 |

---

下一阶段视觉验收应以已存在的 `tests/test_ui_design_layout.py`、`tests/test_ui_comfort.py`、`tests/test_ui_layers.py` 等浏览器测试为基础，在具备授权、浏览器二进制与不触犯受保护目录限制的测试环境中补充逐页截图；本报告没有把未跑的测试标成成功。

## 6. 第二轮真浏览器复测

第一轮的临时项目因运行器默认目录受保护而无法创建。第二轮将合成夹具放入仓库的隔离测试目录，使用独立 Hub、Agent、账号和数据，浏览器采用 Chrome 和 WebKit。

| 范围 | 修复 |
|---|---|
| 全站工具菜单 | 定位同时考虑视口、滚动容器、顶栏和底部导航。菜单内部滚动保留位置，Esc 先关闭菜单。 |
| 原生弹窗与表单 | Tab 和 Shift+Tab 会把焦点滚入可见区域。可视视口缩短时，确认弹窗、CLI 选择器和协作抽屉随之调整。 |
| 总览和电脑 | 已接入电脑时将引导折叠为「继续设置」。统计区按内容占位，操作区保留可用高度。电脑卡片将常用操作排在版本诊断前。 |
| 工作台与协作中心 | 编辑器行号列随位数扩展，长项目名在页脚换行。协作抽屉标题可以换行，并为关闭按钮留出空间。 |

- 两个浏览器、两套主题、六档视口覆盖 20 条主路由，共 480 组几何检查，默认状态没有发现页面横向溢出。 另外通过界面入口检查设置、审计、网关、开发工具、工作台和 CLI 的 26 个子页或浮层，两种浏览器共 52 次。
- 107 项布局、键盘与设计规范测试通过，另有 11 项界面统一性测试和 29 项浏览器扩展测试通过。面板构建产物、静态语法和差异检查均已核对。
- 另一个包含房间、工作流和网关联调的组合批次执行到最后一例后退出超时，未计入通过数。
- 实体手机软键盘与依赖实际外部连接的弹窗状态仍待真机或联调验证。

## 7. 第三轮边界复查

- Chrome 与 WebKit 都复现了三种边界问题。折叠菜单打开的弹窗会跳过隐藏字段，关闭后回到可见入口。工具菜单的最小宽度受所在滚动容器限制。平板侧边抽屉按可视视口缩短，并把当前输入保留在可见范围。
- 房间设置切到诊断时，抽屉重置滚动和焦点。读取失败可就地重试，迟到的结果按抽屉及房间隔离。重试替换内容时保留键盘焦点。
- 共 126 项浮层、设计规范及异步作用域测试通过。4 项 Chrome 与 WebKit 房间抽屉回归通过，其中两项覆盖嵌套诊断失败与重试。移动软键盘几何由浏览器测试模拟。
- 发布前追加验证了协作房间的「查看新消息」。跳转后焦点转到消息列表，迟到的旧滚动回调在已经到达底部时不再重新显示按钮。切换房间会隔离旧读取结果。
