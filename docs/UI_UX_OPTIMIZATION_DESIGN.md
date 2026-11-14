# CodePier (MCP) 全局 UI/UX 精致化与极致体验优化设计文档

> **项目工作区**：CodePier (`MCP` · `main` · `v1.33.15`)
> **覆盖范围**：全站 20 个主路由、81+ 份前端源码（`web/` 下的共享 Token、主面板、远程工作台、原生 CLI 会话 `web/chat/`、房间协作中心 `web/collaboration-room/`、ChatGPT 内嵌 MCP Apps `web/mcp-apps/`、浏览器扩展 `web/browser-extension/`）
> **设计目标**：在严守现有安全边界、幂等恢复机制与零外部前端依赖的前提下，消除历史演进留下的样式断层与交互摩擦，实现**视觉更精致、排版更考究、细节更到位、体验更丝滑**的现代开发面板。
> **实施状态**：第 9 节为历史实施记录；后续落地与验收以第 10 节为准，未提交改动的深度审查与修复见第 11 节（2026-11-14）。

---

## 1. 全景扫描与现状诊断

### 1.1 前端架构与视觉资产盘点

通过对 `web/` 目录全量源码的扫描，当前前端由 **5 大视觉子系统** 组成：

| 子系统 | 核心源码 | 当前视觉与架构特征 |
| --- | --- | --- |
| **1. 核心面板与管理台** | `tokens.css`、`styles.css`、`workspace.css`、`app.js`、`ui.js`、`core/ui.mjs` | Editorial Workspace 风格（暖灰底 `#f4f4f4` / 深色 `#191919` + 品牌黑金 `#292929` / `#f3d970` + 实体封面柔和色 `lilac`/`sky`/`apricot`）。覆盖总览、电脑、项目、VPS、工作台、审计、设置等主路由。 |
| **2. 原生 CLI 会话工作区** | `chat.css`、`chat/styles/*.css`、`chat*.js` | 独立全屏模式（`body.chat-mode`），通过 `--chat-*` 桥接 `--ui-*` 变量並保留 149 项独立加载 Hex 回退白名单。含左侧历史抽屉、居中对话流、底部浮动 Composer、右侧 300px Inspector。 |
| **3. 房间协作中心** | `collaboration-room.css`、`collaboration-room/styles/*.css`、`collaboration-room/*.mjs` | Seamless Split-View 架构（去嵌套盒模型、单像素分割线与色调平面），含 260px 房间侧栏、主对话流、浮动 Composer、右侧 460px 停靠式 `<dialog class="cr-panel">` 抽屉。 |
| **4. 专项业务模块** | `call-log.*`、`queue.*`、`settings.*`、`integrations.*`、`integration-flow.css`、`mcp-gateway.*`、`project-runtimes.*`、`token-usage.*`、`computer.*`、`panel-update.*` | 随路由按需或首屏加载，各自维护局部卡片、表格、时间轴、流式日志与浮层样式。 |
| **5. 独立端侧小应用** | `web/mcp-apps/app.css`（ChatGPT 内嵌变更审阅）、`web/browser-extension/popup.css`（浏览器扩展） | 独立 HTML/CSS 入口，目前使用硬编码颜色与字号，尚未完全接入 `tokens.css` 的视觉尺度。 |

### 1.2 现有基础优势（需严格保留的基石）

在近期的工程演进（如 `UI_DESIGN_SYSTEM_IMPLEMENTATION`、`SITE_EXPERIENCE_UNIFICATION`、`UI_LAYOUT_DEEP_AUDIT` 与 `UX_FLOW_REVIEW`）中，项目已建立起坚实的基础：
1. **自动化设计护栏**：`tests/test_ui_design_guard.py` 与 `tests/test_ui_layout_contracts.py` 锁定了非白名单 Hex 颜色、`<12px` 小字号、全局 Z-Index 层级栈（`100`–`900`）与关键响应式斷点。
2. **主流程初步减负**：侧栏已从 20 个平铺项聚合为 4 大分组 13 个顶层入口 + 页内二级 Tab（`connect` 与 `members`）；接入电脑已支持自动识别平台与监听上线；项目权限已合并为「AI 可以」三档下拉框。
3. **无障碍与视口韧性**：全站支持 `visualViewport` 软键盘避让、`prefers-reduced-motion`、`prefers-contrast: more`、`forced-colors: active` 以及模态框焦点环管理。

### 1.3 核心痛点：为什么整体还不够「精致」？

尽管功能完整、护栏严密，但深入审视 CSS 细节与各路由 DOM 结构后，仍存在 **6 大影响精致度与用户体验的核心短板**：

#### 痛点 A：多代样式叠加导致「同站不同相」（Visual Fragmentation）
- **文件内前后覆盖**：`styles.css`（4582 行）与 `workspace.css`（1365 行）经历了多轮追加式迭代。例如 `.login-screen` 在 `styles.css:2461` 定义为双栏画幅（带 `.login-art` 与 `.login-orbit`），而在 `workspace.css:1012` 被重定义为单列 `440px` 居中卡片；`.overview-summary` 在 `workspace.css:768` 定义为紫色封面，紧接着在 `workspace.css:865` 又被覆盖为白底面板。
- **组件原语分裂**：同一「分段切换」模式在全站有 **5 套截然不同的外观**：
  1. 主面板 `.tabs`（药丸底 + 反色选中块）
  2. 父子页面 `.page-tabs`（下边框 + 浅圆角背景块）
  3. 管理页 `.management-tabs`（底边 3px 粗横线）
  4. 开发工具 `.integration-tabs`（浅灰容器 + 白底阴影滑块）
  5. 设置中心 `.settings-groups`（独立描边药丸按钮）
- **表单控件边框过重**：`styles.css:4128` 将输入控件边框设为 `--ui-control-border: #878787`，在 `#ffffff` 面板上对比度过强、显得生硬粗糙，而协作中心（`cr-field`）与 CLI 会话则使用了柔和的 `--ui-line` / `--ui-border` 搭配浅底色，导致跨页面切换时视觉质感割裂。

#### 痛点 B：排版韵律、对齐基准与空间呼吸感不足（Typography & Rhythm）
- **中西文混排与行高断层**：标题、正文、代码与微标签混用多种行高（`0.92`、`1.04`、`1.25`、`1.55`、`1.8`），缺少统一的 `text-wrap: balance`（标题）与 `text-wrap: pretty`（正文说明）。
- **卡片内部对齐失序**：
  - 项目卡片（`.workspace-project-row`）头部高 `124px`、使用 `28px` 大号标题，中间路径与标签却骤降为 `12px`，底部操作栏同时塞入「打开 + Pi + Codex + Claude + 更多」5 个按钮，在 2/3 列网格下频繁折行，底边参差不齐。
  - 电脑卡片（`.workspace-device`）、VPS 卡片（`.vps-card`）、开发工具卡片（`.integration-action-card`）的内边距分別为 `20px`、`22px`、`24px`，圆角混用 `16px` 与 `20px`，缺乏统一的网格模数。
- **数据对齐与等宽细节缺失**：时间戳、操作耗时、SHA 指纹、端口号、配额百分比等未全面启用 `font-variant-numeric: tabular-nums` 与精致的药丸代码框，长哈希或路径截断缺乏渐隐或悬浮复制体验。

#### 痛点 C：高频工作台（Workbench）停留在「简陋表单」而非「精致 IDE」（Workbench UX）
- **固定死板的双栏**：`.workspace` 固定 `grid-template-columns: 230px minmax(0, 1fr)`，无法拖拽调节目录树宽度，也没有文件面包屑或打开文件状态条。
- **原生 `<textarea>` 编辑体验单薄**：缺少当前行高亮、缩进参考线、查找替换栏、行尾/编码状态微交互；底部任务区（`.taskbar`）与 `.terminal`（最大高度仅 `64px`–`128px`）被挤压在页面最下方，看日志极其局促。
- **Diff 审阅粗糙**：`.diff` 仅对 `+`/`-` 整行染色，缺少双列行号（旧行号/新行号）、文件头折叠栏与词级（word-level）差异高亮。

#### 痛点 D：协作中心与 CLI 会话的认知负荷仍偏高（Cognitive Load）
- **协作中心权限矩阵复杂**：尽管 `UX_FLOW_REVIEW_PART2` 已指出问题，当前 `collaboration-room/permissions.mjs` 仍存在大量复选框组合、渠道重复配置与冗长状态枚举；工作流（Work）与对话流（Feed）的视觉层次尚不够轻盈。
- **CLI 会话入口与设置分散**：项目卡片上仍并列展示 Pi / Codex / Claude 三个按钮；会话内部模型、思考强度、工作目录与会话设置抽屉存在多处入口重叠；工具调用审批（Approval）卡片与执行过程（Execution）折叠块的视觉精致度有待提升。

#### 痛点 E：文案「防御性免责声明」与内部术语过多（Microcopy & Tone）
- 页面中仍有大量 `.form-note`、`.notice`、`.page-help` 充斥着“不是沙箱、不自动执行、不代表已送达、请核对原操作回执”等工程免责句式，喧宾夺主，稀释了真正关键的操作引导。
- 裸露的 `#ab12cd34` 短 ID、`v3 · checkpoint`、原始错误码等内部实现细节直接暴露在主视图中。

#### 痛点 F：独立端侧应用（MCP Apps & 浏览器扩展）脱离主设计系统
- `web/mcp-apps/app.css` 与 `web/browser-extension/popup.css` 仍采用早期硬编码样式（如 `font-weight: 650`、`10px` 字号、生硬的 `#ccc` 边框），在 ChatGPT 内嵌审阅代码变更或点击浏览器扩展时，缺乏 CodePier 品牌的精致感。

---

## 2. 核心设计理念与美学北极星

本轮优化遵循 **「Editorial Precision · 精密编排的现代工作台」** 设计哲学，对标 Linear、Raycast、Vercel Dashboard 与 Zed 的精致度标准：

1. **克制的层次（Whisper-quiet Hierarchy）**
   少用高对比粗黑框线，多用 **微色阶表面（Tonal Surfaces）+ 1px 柔和发丝线（Hairline Borders）+ 精细内高光（Subtle Inner Highlights）** 来区分容器层级。
2. **极致的排版秩序（Typographic & Grid Discipline）**
   所有尺寸严格收敛至 `4px` 基准网格；标题紧凑有力，正文舒适易读，数字与代码严格等宽对齐；每一张卡片的头部、主体、底栏在多列网格中严格水平对齐。
3. **渐进式披露（Progressive Disclosure）**
   首屏永远只展示 **1 个核心状态 + 1 个主行动点（Primary CTA）+ 2~3 项关键元数据**。高级参数、原始 JSON、长哈希、免责边界说明全部折叠进二级抽屉、Popover 或 Tooltip。
4. **确定性微交互（Deterministic Micro-interactions）**
   每一次点击、复制、筛选、保存、异步等待（`pending`）都有即时、克制、无布局抖动（Zero CLS）的视觉反馈。

---

## 3. 设计系统（Design System）2.0 升级方案

> **护栏兼容承诺**：所有 Token 演进均在 `web/tokens.css` 内完成，保持 `tests/test_ui_design_guard.py` 中 `unapproved_hex <= 3`、`font_px_below_12 == 0`、`font_size_distinct <= 9`、`radius_distinct <= 8`、`weight_distinct <= 4` 零违规。

### 3.1 色彩与表面质感精调（Surface & Border Refinement）

当前 `--ui-control-border: #878787` 过重，且卡片缺少细腻的立体边缘。建议在 `web/tokens.css` 中优化色阶配比：

| Token | 现状 (Light / Dark) | 优化方案 (Light / Dark) | 视觉改进效果 |
| --- | --- | --- | --- |
| `--ui-bg` | `#f4f4f4` / `#191919` | `#f5f5f4` / `#141413` | 更温润的暖白纸张感与更深邃的暗色画布，拉开与面板的层级对比 |
| `--ui-panel` | `#ffffff` / `#222222` | `#ffffff` / `#1c1c1b` | 提升暗色模式下卡片与背景的纯净度 |
| `--ui-side` | `#fafafa` / `#1e1e1e` | `#fafaf9` / `#181817` | 侧栏与次级抽屉底色更自然地融入主画布 |
| `--ui-line` | `#e3e3e3` / `#3a3a3a` | `#e6e5e3` / `#2c2c2a` | 表格分割线与容器发丝线更克制，消除“网格铁丝网”感 |
| `--ui-border` | `#cccccc` / `#515151` | `#d6d5d2` / `#3d3d3a` | 按钮与悬停卡片描边更细腻 |
| `--ui-control-border` | `#878787` / `#818181` | `#cac8c3` / `#4a4a46` | **关键修复**：消除输入框刺眼的深灰粗框，改为平时温润、悬停加深、聚焦高亮 |
| `--ui-elev-1` | `0 1px 3px rgb(0 0 0 / 4%)` | `0 1px 2px rgb(0 0 0 / 4%), 0 0 0 1px rgb(0 0 0 / 2%)` | 为卡片赋予微米级边缘清晰度 |

### 3.2 字体排版与中西文韵律规范（Typography Scale）

保持现有 9 档字号与 4 档字重 Token 不新增，但规范各层级的组合契约：

| 层级角色 | 字号 Token | 字重 Token | 行高与字距规范 | 应用场景 |
| --- | --- | --- | --- | --- |
| **大数字指标 (Metric Display)** | `--ui-text-display` (`36px`) | `--ui-weight-semibold` (`600`) | `line-height: 1.0`, `letter-spacing: -0.035em`, `tabular-nums` | 总览统计卡、Token 总额、产物大小 |
| **页面主标题 (Page H1)** | `--ui-page-title` (`28px` / 移动 `26px`) | `--ui-weight-bold` (`700`) | `line-height: 1.2`, `letter-spacing: -0.025em`, `text-wrap: balance` | 各路由顶部 `.page-head h1` |
| **大卡片标题 (Entity H2)** | `--ui-text-h2` (`22px`) | `--ui-weight-semibold` (`600`) | `line-height: 1.25`, `letter-spacing: -0.02em` | 电脑卡、项目卡主名称、登录标题 |
| **面板/弹窗标题 (Section H2)** | `--ui-text-title` (`18px`) | `--ui-weight-semibold` (`600`) | `line-height: 1.35`, `letter-spacing: -0.015em` | `.panel-head h2`、`.modal-header h2`、抽屉标题 |
| **正文与表单标题 (Subhead)** | `--ui-text-md` (`15px`) / `--ui-text-input` (`16px`) | `--ui-weight-semibold` (`600`) | `line-height: 1.5` | 列表项主标题、表单分组标题、移动端防缩放输入 |
| **标准正文 (Body)** | `--ui-text-base` (`14px`) | `--ui-weight-regular` (`400`) | `line-height: 1.6`, `text-wrap: pretty` | 页面说明、消息气泡、设置描述 |
| **紧凑控件与表格 (UI Dense)** | `--ui-text-sm` (`13px`) | `--ui-weight-medium` (`500`) | `line-height: 1.45` | 导航项、按钮、表格正文、表单 Label |
| **辅助元数据与徽章 (Caption)** | `--ui-text-xs` (`12px`) | `--ui-weight-medium` (`500`) | `line-height: 1.4`, `tabular-nums` | 状态徽章、时间戳、路径、快捷键提示 |

### 3.3 空间模数与对齐准则（Spatial Grid & Alignment）

1. **三档卡片内边距标准**：
   - **大画幅/重点容器**（总览概览卡、弹窗 Body）：`24px`（`--ui-space-6`）
   - **标准实体卡片/面板**（项目卡、电脑卡、VPS 卡、设置卡）：统一为 `20px`（`--ui-panel-inset`），消除 `17px`、`18px`、`22px`、`24px` 混用。
   - **紧凑列表行/内嵌块**（最近操作行、授权行、审计条目）：`12px 16px`（`--ui-space-3` `--ui-space-4`）。
2. **多列卡片底边对齐契约**：
   - 所有卡片网格（`.workspace-project-collection`、`.workspace-device-collection`、`.vps-grid`）的子项统一设置为 `display: flex; flex-direction: column;`，主体描述区设置 `flex: 1`，底部操作区（`footer` / `.actions`）设置 `margin-top: auto`，确保同行卡片底栏按钮严格处在同一水平线上。

---

## 4. 全局骨架与基础组件精致化（Shell & Primitives）

### 4.1 侧边导航栏（Sidebar）减噪与呼吸感提升
- **移除冗余搜索框**：桌面端侧栏顶部的 `.nav-search`（「查找页面」）与顶栏右侧的 `⌘K`（「快速前往」）功能完全重叠，且占据了宝贵的纵向空间。**方案**：桌面端移除 `.nav-search`，将空间切换器（`.iam-space-switch`）整合为品牌区下方的精致下拉胶囊；移动端抽屉可保留顶部快速筛选或直接调起命令面板。
- **分组与选中态精修**：
  - `.nav-group + .nav-group` 增加 `8px` 顶部间距与隐形分割感，`.nav-label` 采用 `--ui-text-xs` + `--ui-soft` 弱化视觉抢夺。
  - `.nav button.active` 左侧增加 `3px` 圆角指示条或保持柔和暖金底色（`--ui-sidebar-active`），右侧小圆点 `::after` 改为仅在「该路由有进行中任务/未读事件」时作为动态状态点显示，赋予实际语义。
- **底部个人信息栏（`.side-bottom`）一体化**：
  - 将「实时通道状态（`#event-state`）」、「外观切换」与「个人账号（`.user-box`）」整合为紧凑的底部控制舱：通道正常时仅在头像或状态栏显示一颗呼吸绿点 `● 实时同步`，仅在断线重连时展开醒目黄条提示，释放侧栏底部约 `56px` 的固定高度。

### 4.2 顶栏（Topbar）与二级导航（Page Tabs）统一
- **面包屑层级化**：当处于二级 Tab 页面（如 `连接 → 访问 Profiles` 或 `成员与权限 → 角色`）时，顶栏面包屑动态显示为 `CodePier / 连接 / 访问配置`，并提供平滑的层级感。
- **全站统一为 2 套 Tab 原语（消灭 5 套分裂实现）**：
  1. **主级页面/分区切换（Underline Bar Tabs）**：统一 `.page-tabs` 与 `.management-tabs`。采用底边 `1px solid var(--ui-line)`，选中项带 `2px solid var(--ui-text)` 底部指示条与字重加粗，用于页面级路由或主模块分区（如连接页、成员页、MCP 网关页）。
  2. **容器内视图/筛选切换（Segmented Pill Control）**：统一 `.tabs`、`.integration-tabs`、`.cr-connection-mode`。采用 `--ui-surface` 圆角槽底（`padding: 3px; border-radius: var(--ui-radius-md)`），选中项为 `--ui-panel` 背景 + `--ui-elev-1` 微阴影，用于「工具执行 / 全部事件」、「文件 / 编辑器」、「Pi / Codex / Claude」等局部切换。

### 4.3 按钮、徽章、输入控件与表格微整形
- **按钮（`.btn`）**：
  - Primary 按钮（`--ui-primary: #f3d970`）增加微弱的顶部内高光 `box-shadow: inset 0 1px 0 rgb(255 255 255 / 35%), var(--ui-elev-1)`，悬停时微升或加深色阶，消除平涂色块的廉价感。
  - 统一卡片内次要操作按钮高度为 `--ui-control-compact` (`36px`，触屏自动 `44px`)，避免卡片内部出现 `40px` 大按钮与卡片标题争抢视觉焦点。
- **状态徽章（`.badge`）**：
  - 为运行中/重连中徽章（`.badge.running`、`.badge.reconnecting`）内置一颗 `6px` 柔和脉动圆点，静态完成/只读徽章保持克制的低饱和背景，做到「一眼区分动静」。
- **输入与选择框（`.field`）**：
  - 默认态：`background: var(--ui-panel); border: 1px solid var(--ui-control-border)`（使用调柔和后的 `#cac8c3`）。
  - 悬停态：`border-color: var(--ui-muted)`。
  - 聚焦态：`border-color: var(--ui-accent); outline: 3px solid color-mix(in srgb, var(--ui-focus) 22%, transparent); outline-offset: 0`（比外扩 3px 实线框更现代、更贴合圆角，同时在 `forced-colors` 下回退为标准实线 `outline`）。
- **数据表格（`.data-table`）**：
  - 表头 `th` 增加微弱底色 `--ui-side` 与固定高度 `40px`，数字与时间列右对齐或等宽左对齐。
  - 操作列（最后一列 `td:last-child`）宽度固定为 `1%`、`white-space: nowrap; text-align: right;`，箭头图标按钮在行悬停时浮现边框与微位移反馈（`transform: translateX(2px)`）。

### 4.4 浮层、弹窗（Modal）与确认单（ConfirmSheet）体验升级
- **消除最后 12 处原生 `window.confirm()`**：
  - 目前在 `app.js`（工作台脏草稿离开、切换项目）、`identity.js`（切换空间）、`core/ui.mjs`（关闭未保存编辑器弹窗）等处仍残留 12 处浏览器原生同步 `confirm()`，在深色模式与精致 UI 中极其突兀。
  - **改造方案**：将路由切换守卫（`navigate`）、项目切换守卫（`#work-project.onchange`）与弹窗关闭守卫（`requestClose`）升级为异步守卫，统一调用品牌化的 `CP.ui.confirmSheet()`，并将 `test_ui_design_guard.py` 中的 `native_confirm_calls` 基线从 `12` 降至 `0`。
- **大尺寸编辑弹窗（`.modal-editor`）布局优化**：
  - 头部与底栏采用半透明毛玻璃或微阴影粘性固定（Sticky Header/Footer），表单滚动区顶部/底部增加滚动阴影提示，防止长表单（如编辑角色、VPS、项目运行时）隐藏下方重要字段时用户无感知。

---

## 5. 核心业务页面逐页 UI/UX 深度优化方案

### 5.1 登录与首次引导（Login & Onboarding）
- **清理 CSS 双重定义冲突**：移除 `styles.css` 中已废弃的 `.login-art` / `.login-orbit` 冗余规则，以干净、克制的居中登录卡片为唯一标准。
- **登录卡精修**：
  - 顶部左右两端对齐品牌标识（`.brand`）与外观切换器（`.appearance-control`），标题 `h1` 简化为「登录工作区」。
  - 外部身份登录按钮（OIDC）与本地账号表单之间增加优雅的「`── 或使用账号密码 ──`」分割线，而非生硬堆叠。
- **总览页新用户引导（`.onboarding`）**：
  - 将现有的 `<ol>` 纯文字列表升级为 **水平三步进度条卡片（Stepper Card）**：
    - `① 接入电脑` → `② 添加项目` → `③ 连接 ChatGPT`
    - 已完成步骤显示绿色对勾 `✓` 与已绑定的电脑/项目名；当前步骤高亮展开操作区；未开始步骤置灰预览。全部完成后平滑收起。

### 5.2 总览仪表盘（Overview · `overviewHTML`）
- **清理 `.overview-summary` 样式覆盖**：在 `workspace.css` 中合并两处重复定义，固定为干净的白色/深灰面板容器，内部 4 格统计（在线电脑、项目、今日操作、执行中）采用 `1px` 竖向分割线。
- **Token 用量与费用估算（`token-usage.js` / `token-usage.css`）**：
  - 当前 Token 仪表盘嵌在总览顶部卡片下半部分，当展开筛选器、价格说明、缓存假设与每日趋势列表（`.token-trend`）时，会将下方的「最近操作」和「项目入口」挤出首屏。
  - **优化方案**：默认态在总览顶部卡片底部仅展示 **紧凑单行摘要条**（`近 30 天用量：1.24M tokens · 预估 $1.85 · 缓存命中假设 95% [展开趋势与明细 ▾]`），点击后再平滑展开筛选器与柱状趋势图，确保 `1366×768` / `1440×900` 笔记本首屏完整展示「统计 + 最近操作 + 项目入口」。
- **项目快捷入口（`.workspace-recent-projects`）**：
  - 移除意义不明的圆形序号 `01`、`02`（`.project-index`），改为项目文件夹状态图标（在线绿点/离线灰点）；恢复显示单行省略的本机路径 `p`（当前在 `workspace.css:322` 被 `display: none` 隐藏，导致同名或相近项目无法区分路径）。

### 5.3 项目与电脑卡片网格（Projects & Devices）
- **项目卡片（`.workspace-project-row`）精细重构**：
  - **头部减负**：将 `.workspace-project-head` 最小高度从 `124px` 降至 `88px`，标题字号从 `28px`（`--ui-page-title`）收敛为 `20px`/`22px`（`--ui-text-h2`），避免长项目名（如 `mcp-gateway-service-prod`）断成三行。
  - **元数据标签化**：将「设备名 + 文件权限 + 任务执行」整合为紧凑的一行图标徽章条，路径 `<code>` 旁增加悬浮一键复制小按钮。
  - **底栏操作收敛（解决按钮折行）**：
    - 现状：`[打开] [Pi] [Codex] [Claude] [更多]` 共 5 个按钮挤在卡片底部。
    - 优化：底栏只保留 **3 个元素**：左侧主按钮 `[打开工作台 →]` + 次按钮 `[CLI 对话]`（点击直接进入上次使用的 CLI 或弹出轻量三选一）+ 右侧 `[⋯ 更多]` 菜单。卡片底栏永远单行整齐排列。
- **电脑卡片（`.workspace-device`）**：
  - 头部高度从 `116px` 收敛至 `92px`，移除右侧图标刻意旋转 `transform: rotate(8deg)` 的突兀装饰，改为端正的平台专属图标（macOS / Windows / Linux 标识）。
  - 将卡片底部的 `.device-lifecycle`（Agent 版本与服务状态）与上方的 2×2 元数据网格融合，消除卡片内部多条横线切割的碎片感。

### 5.4 远程工作台（Workbench · `workbenchHTML`）IDE 级体验进化
- **可调节分栏与紧凑顶栏**：
  - 将「项目选择器 + 隔离目录状态 + Git 状态/差异/备份 + 测试验收/检索/技能」整合为一条高密度的 **IDE 顶部工具栏（Workbench Header Bar）**，减少纵向占用的 2 行空白。
  - `.workspace` 左右分栏引入轻量 CSS 变量宽度 `-- explorer-width: 240px` 与拖拽分隔条（Resizer），支持双击折叠/展开文件树。
- **文件树（`.file-tree`）细节**：
  - 目录与文件节点高度微调为 `32px`（桌面端紧凑型） / `40px`（触屏端），左侧层级缩进增加 `1px` 纵向引导线（Indent Guide），选中态采用柔和的 `--ui-accent-soft` + 左侧強調条，而非刺眼的整行纯黑反色块（`.file-node.selected { background: var(--ui-text); color: var(--ui-panel); }`）。
- **代码编辑器与底部控制台（`.editor` & `.terminal`）**：
  - 编辑器顶部 `.editor-top` 显示可点击的路径面包屑 `src / components / app.js` 与醒目的未保存圆点 `● 未保存`。
  - **底部任务与输出抽屉化**：当前桌面端 `.terminal pre` 被限制为 `max-height: 64px`（`styles.css:4289`），几乎无法阅读任何报错日志！改为 **可折叠/可切换高度（折叠 36px / 常规 180px / 最大化 50dvh）的底部输出面板**，任务运行中自动展开至 `180px` 并自动滚底。
- **Diff 预览弹窗（`diffHTML`）**：
  - 增加统计进度条（`+12 −4` 绿红比例块），代码区支持双栏行号与行内高亮，长无变化区块视觉弱化。

### 5.5 原生 CLI 会话（CLI Sessions · `web/chat/`）
- **新建会话与配置合一**：
  - 项目选择弹窗顶部内置Segmented Control（`Pi` | `Codex` | `Claude Code`），选项目的同时即可切换 CLI，无需进会话后再点右下角齿轮。
  - 底部 Composer 下方的配置栏（`.chat-config-bar`）将 `CLI · 模型 · 思考强度 · 工作目录` 整合为精致的膠囊触发器，悬停显示完整信息，点击弹出就地 Popover。
- **消息流与工具执行块（`.chat-message` & `execution.css`）**：
  - 用户消息气泡限制优雅阅读宽度（`max-width: min(78%, 620px)`），AI 回复保持 `76ch` 最佳阅读行宽。
  - 代码块头部（`.chat-code-head`）粘性吸顶（`position: sticky; top: 0`），方便长代码滚动时随时一键复制。
  - 审批请求卡片（`approvals.js`）增加清晰的「执行命令 / 作用目录 / 风险提示」结构化键值表，主次按钮对比鲜明。
- **快捷键统一**：在 `chat-mode` 下按 `⌘K` 统一打开包含全局路由跳转与当前会话指令的命令面板，消除 `ui.js` 与 `chat-chrome.js` 的监听冲突。

### 5.6 房间协作中心（Collaboration Center · `web/collaboration-room/`）
- **建房与加人零阻碍闭环**：
  - 新建房间抽屉直接提供「关联项目（可多选，默认只读）」列表，创建成功后自动初始化房间权限配置；点击「添加 dot」时不再因未手动保存权限页而报错拦截，优先展示「复用已有在线 dot」一键加入。
- **权限抽屉（`permissions.mjs`）极简化**：
  - 废除每行 6 个复选框 + 18 个批量按钮的复杂矩阵，改为 **每行一个项目名称 + 一个四档权限下拉框（`不加入` / `只读` / `读写` / `读写和运行`）**；超出电脑上限的档位自动 `disabled` 并提示原因。
  - 微信默认勾选「使用与房间相同的项目权限」，仅在取消勾选时展开自定义列表；未启用的 Telegram 通道彻底隐藏。
- **主对话舞台（Stage & Feed）减噪**：
  - 移除消息流顶部/底部的「消息窗口 50/200 条」「工作窗口 x/y 项」等内部调试提示。
  - 将原本堆叠在消息流最底部的 `.cr-work-list` 提炼为 Composer 上方的 **浮动进行中工作胶囊条（Active Work Bar）**，点击在右侧抽屉查看步骤详情，确保最新聊天消息永远紧贴输入框上方。
  - 微信向导收敛为「扫码 → 确认范围与绑定 → 发送配对码自动完成」，所有原始 `wxid_` / `peer_id` 优先展示昵称或前 4 后 4 脱敏标识。

### 5.7 连接、权限、审计与设置中心（Connect, IAM, Audit & Settings）
- **连接页（`connectHTML` & `connection-setup.js`）**：
  - 首屏聚焦「ChatGPT MCP 地址一键复制」与「当前有效连接列表」；将「连接自检表单」简化为一行状态卡片 + `[测试连接]` 按钮，目录 SHA 比对与 Secure MCP Tunnel 统一收入高级折叠区。
  - 授权同意弹窗（`consentModal`）提供 `[只允许读取]` 与 `[允许读写和运行]` 两个明确的一键预设按钮，将期限、Profile 与技术参数收进 `<details class="form-advanced">`。
- **操作审计与实时队列（`call-log.js` & `queue.js`）**：
  - 筛选栏（`.call-filters`）采用紧凑的一行式布局：`[工具执行 | 全部事件] [项目筛选 ▾] [工具类型 ▾] [状态 ▾] [搜索框]`。
  - 审计条目（`.call-entry`）摘要行采用固定网格列宽或弹性右对齐状态区，使「耗时」与「状态徽章」在纵向列表上形成整齐的视觉列，不再随参数预览长短而左右乱跳。
- **设置中心（`settings.js` & `settings.css`）**：
  - 顶部 5 张任务引导卡（`.settings-flow-card`）在已有配置完成时支持折叠为一行快捷入口，让下方的 8 类设置分类标签与设置项直接进入首屏视野。

### 5.8 端侧应用视觉统一（MCP Apps & Browser Extension）
- **`web/mcp-apps/app.css`（ChatGPT 内嵌变更审阅）**：
  - 对齐 `tokens.css` 的浅/深色背景、边框、语义色与圆角；移除 `font-size: 10px`、`11px` 与 `font-weight: 650`，全部升级至 `>=12px` 与标准字重，优化文件折叠块与 Diff 代码框的内边距与滚动体验。
- **`web/browser-extension/popup.css`（浏览器扩展）**：
  - 移除文件上半部分旧的 `#ccc` / `#574ac0` 蓝色焦点环与下半部分覆盖规则的重复写法，整合为一套干净的、基于 `--ui-*` 变量的设计样式，加入精致的品牌头部与连接状态指示点。

---

## 6. 文案与微交互去「AI 味」规范（Microcopy Polish）

1. **三不写原则**：
   - 用户没问的「不会发生的事」不写（删去成片的“不会删除文件、不调用模型、不自动扩权”連环否定句）。
   - 系统内部正常运转的机制不写（删去“基于版本 N 保存”、“按原操作编号核对”等底层协议描述，仅在网络中断不确定时给出友好的`[重试]`按钮）。
   - 正常状态下的故障排错说明不写（如首次安装电脑时不写 Windows 损坏恢复指南，仅在报错时按需展示）。
2. **全站术语唯一化**：
   - `设备 / 节点 / 本机 / 家里电脑` → 统一为 **电脑**
   - `项目映射 / 映射` → 统一为 **项目**
   - `凭证 / 授权 / Grant` → 统一为 **连接**（OAuth）与 **访问令牌**（PAT）
   - `核对 / 核实 / 验收 / 证据 / 回执` → 统一为 **检查 / 确认 / 结果**
   - 全站 6 种不同叫法的幂等重试按钮 → 统一为 **重试**（`title="沿用原请求，不会重复执行"`）
3. **短 ID 与哈希隐藏策略**：
   - 主界面列表中不再直接拼接 `#ab12cd34`（同名冲突除外）；操作编号、SHA256、连接 ID 统一使用紧凑胶囊 `<code>` 展示前 8 位，点击即可复制完整值并弹出 `已复制` 轻提示。

---

## 7. 分阶段实施路线图与验收标准

为确保改动安全、可回滚且不破坏现有 500+ 项自动化测试，建议分 **3 个阶段（Phases）** 推进：

| 阶段 | 聚焦目标 | 涉及核心文件 | 关键交付物与验收标准 |
| --- | --- | --- | --- |
| **Phase 1：设计系统 2.0 与全局骨架精修** | 统一视觉 Token、清理重复覆盖 CSS、统一 2 套 Tab 原语、优化表单边框与聚焦态、消灭最后 12 处原生 `confirm()`、升级 MCP Apps 与浏览器扩展样式 | `web/tokens.css`、`web/styles.css`、`web/workspace.css`、`web/ui.js`、`web/core/ui.mjs`、`web/mcp-apps/app.css`、`web/browser-extension/popup.css` | 1. `tests/test_ui_design_guard.py` 全部通过，且 `native_confirm_calls` 降至 `0`；<br>2. `scripts/css_override_report.py` 跨文件与同文件冲突清零；<br>3. 浅/深色模式下所有表单、按钮、Tab、卡片边框呈现统一的 Editorial 质感。 |
| **Phase 2：高频主界面排版与 IDE 工作台升级** | 总览首屏减负（Token 面板折叠化 + 三步引导条）、项目/电脑/VPS 卡片底栏对齐与按钮收敛、工作台可调分栏与可伸缩终端输出面板、审计列表对齐优化 | `web/app.js`、`web/token-usage.*`、`web/vps.*`、`web/call-log.*`、`web/queue.*`、`web/settings.*` | 1. `1366×768` 与 `1440×900` 桌面视口下，总览页首屏无需滚动即可看全核心板块；<br>2. 项目卡片在 2/3 列网格下底栏按钮 100% 单行水平对齐；<br>3. 工作台终端输出支持折叠/展开/最大化，代码与 Diff 审阅清晰易读。 |
| **Phase 3：协作中心、CLI 会话流线化与全站文案净水** | 协作中心建房选项目、权限单一下拉框、进行中工作胶囊条、微信向导减步；CLI 会话新建选 CLI 与配置栏合一；全站去 AI 味文案与 `check-ui-copy.mjs` 护栏落地 | `web/collaboration-room/*`、`web/chat/*`、`web/chat*.js`、`web/workflows.js`、`web/agent-install.js`、`scripts/check-ui-copy.mjs` | 1. 首次接入、建房协作、新建 CLI 会话点击次数减少 40%+；<br>2. Chromium + WebKit × 浅/深色 × 6 档视口（`320`/`390`/`768`/`1024`/`1366`/`1440`）0 横向溢出、0 触控目标小于 `44px`；<br>3. `scripts/build_web_assets.py --check` 与全套 UI 浏览器回归测试 100% 通过。 |

---

## 8. 工程与安全约束清单（实施红线）

在后续按本设计文档编写代码时，必须严格遵守以下红线：
1. **权限与安全零妥协**：UI 简化（如一键预设按钮、权限下拉框）不得在后台隐式扩大用户未确认的权限范围；`computer`（桌面/浏览器控制）永远不进入默认勾选预设。
2. **幂等与恢复契约不变**：文案上将「核对原操作」简化为「重试」，但底层必须 100% 保留 `idempotency_key`、`operation_id` 轮询与跨会话/跨空间校验（`uiConfirmAction` / `workDialogCurrent`）。
3. **构建与哈希同步**：任何 `web/` 下源码的修改，完成后必须运行 `python scripts/build_web_assets.py`（以及 `web/mcp-apps/build.mjs` 如涉及内嵌模块）更新 `web/index.html` 与 `web/assets-manifest.json`，严禁手动修改生成产物。

---

## 9. 实施进度

> **核对时间**：2026-11-14（第二轮）· **核对对象**：`main@928680c` 上的未提交改动（尚未提交）
> **第一轮改动备份**：`refs/backup/uiux-baseline-20261114`（`git stash create` 快照，不影响工作区）与 `~/.codepier-backups/uiux-baseline-20261114.patch`
> **图例**：✅ 已完成 · ⚠️ 部分完成 · ❌ 未开始 · ❔ 未逐项核实

### 9.1 总体

| 阶段 | 状态 | 剩余缺口 |
| --- | --- | --- |
| Phase 1：设计系统 2.0 与全局骨架 | ✅ 完成 | — |
| Phase 2：高频主界面与 IDE 工作台 | ✅ 基本完成 | IDE 顶部工具栏整合未做 |
| Phase 3：协作中心、CLI 会话与文案 | ⚠️ 大部分完成 | 权限行内复选框仍保留；微信向导减步、⌘K 统一、配置栏胶囊、全站文案净化未做 |

### 9.2 Phase 1 明细

- ✅ §3.1 色阶与表面、§4.4 原生 `confirm()` 清零（`native_confirm_calls = 0`）、§5.1 登录、§5.2 `.overview-summary` 合并、§5.8 端侧样式、§4.3 输入/按钮/徽章/表格、§4.4 弹窗粘性头尾、§4.2 面包屑：见第一轮记录，均保持。
- ✅ §4.2 Tab 收敛为 2 套原语：`styles.css` 定义「分段控件」（`.tabs` / `.integration-tabs` / `.settings-groups` 共用一组选择器与选中态）与「下划线标签」（`.page-tabs` / `.management-tabs`）。`integrations.css`、`settings.css` 只保留外边距、换行、滚动等布局差异，不再各自定义外观；高对比度模式下选中态描边同步覆盖三者。
- ✅ §4.1 导航状态点：`app.js` 新增 `navActivitySeed/Operation/Sync`，总览给出进行中操作数，实时 `operation` 事件按编号增减；「操作审计」入口仅在有进行中操作时显示状态点并带 `title`。侧栏重绘后由 `uiSyncNavigation` 重新同步。
- ❔ §4.1 空间切换胶囊、底部控制舱断线展开；§4.4 表单滚动阴影（未做视觉核实）。

### 9.3 Phase 2 明细

- ✅ §5.4 Diff：`diffHTML` 拆为 `diffParse` / `diffWordPair` / `diffRowsHTML`。文件头只在 hunk 之外识别（按 `@@` 头的新旧行数判断 hunk 边界，多文件 diff 也正确）；`\ No newline at end of file` 显示为注释行且不推进行号；无 `@@` 的裸文本仍着色但不显示行号。**词级高亮**：相邻的删除/新增行配对，按公共前后缀（对齐到单词边界）标出变化部分 `<mark class="diff-word">`，两行相同部分不足一半时不高亮。新增 `tests/test_diff_html.mjs`（5 例）并加入两个 CI 工作流。
- ✅ §5.4 工作台：修复 1366 以下（`max-width: 1200px`）文件树与编辑器被挤成上下两行的问题——第一轮加入的拖拽分隔条让网格多出一列，而窄屏规则仍是两列模板；现改为窄屏只调 `--explorer-width: 195px`，用户拖拽后才写入行内宽度。1180×640 下编辑器高度恢复正常。
- ✅ §5.2 Token 用量摘要条：折叠时（总览，桌面宽度）标题、两项指标与「查看详情 · 趋势、筛选与明细」排成一行，估算说明收为一行省略文本（悬停显示全文）；展开后恢复原布局。DOM 结构不变，现有测试锁定的指标与说明仍可见。
- ✅ §5.1 新用户三步引导：进度「n / 3 已完成」与进度条；已完成步骤显示已接入的电脑名 / 项目名 / 连接数；未开始步骤显示虚线卡片与一句预览；桌面宽度下步骤间有连接线（完成后变实线）。折叠态摘要显示 `n / 3`。
- ✅ §5.7 审计列表：筛选栏主行为「工具执行 | 全部事件」+ 项目 + 工具 + 状态 + 搜索；来源与触发来源收进「更多筛选」浮层（有生效筛选时显示计数并保持展开）。状态区改为固定宽度两列网格（状态徽章 / 历时），退出码另起一行，纵向列表对齐。
- ✅ §5.7 设置中心引导卡：记住用户的折叠/展开选择（`localStorage`）；未选择过时，若电脑、项目、连接都已就绪则默认折叠。
- ✅ VPS 卡片：底栏单行并加分隔线；备注最多两行；移动端改用标准内边距；「检查连接」不可用时说明原因（已停用 / 未分配项目）。
- ✅ 第一轮遗留问题修复：输出高度按钮 `aria-pressed`；分隔条 `aria-valuenow/min/max` 与 `pointercancel`；`settings.js` 开发工具入口异常现在会提示而非静默；删除无调用方的 `agentExecutionHelp`。
- ❌ §5.4 IDE 顶部工具栏整合（项目选择 + Git + 测试检索合并为一条）。
- ❔ §5.3 `.device-lifecycle` 与元数据网格融合；§5.4 文件树缩进引导线。

### 9.4 Phase 3 明细

- ✅ §5.5 新建会话时选择 CLI：会话设置的「新对话」区在项目旁新增 `Pi | Codex | Claude Code` 分段控件，驱动原有 `#chat-provider`，切换走同一条 `chatSwitch` 路径；使用 `--chat-*` 变量，独立会话页（不加载面板样式）下浅/深色对比度达标。
- ✅ §5.5 审批卡结构化、代码块吸顶、气泡限宽（第一轮）。
- ✅ §5.6 建房选项目（可多选，初始只读）、添加 dot 时自动初始化房间权限而非拦截、进行中工作胶囊条（已在代码中，第一轮记录有误）。
- ⚠️ §5.6 权限：每行已是一个项目名 + 一个档位下拉（不加入 / 只读 / 读写 / 读写和运行），能力复选框已隐藏，Telegram 不支持时隐藏，微信默认「使用和房间相同的权限」。**仍保留**每行的选择复选框与「全选 / 所选权限」批量栏——它们承担批量操作，且约 22 处安全相关的浏览器测试依赖它们；移除需要单独一轮并重写这些测试。
- ✅ §5.7 授权弹窗一键预设：「只允许读取」与「允许读写（和运行）」两个按钮，期限 / Profile / 客户端信息收在「调整期限、项目或权限」中；`computer` 从不进入预设（已在代码中）。
- ✅ §5.7 连接页首屏：顺序改为「ChatGPT 连接地址 → 当前连接列表 → 连接检查」；连接检查为一行（选择连接 + 「测试连接」），项目与目录 SHA 收进「排查选项」，Secure MCP Tunnel 仍在「其他客户端」中。
- ❌ §5.5 配置栏胶囊合一、⌘K 在 chat-mode 统一。
- ❌ §5.6 微信向导减步、`wxid_` / `peer_id` 脱敏展示、默认优先「已有 dot」。
- ⚠️ §6 文案：`check-ui-copy.mjs` 明确问题 0 处，场景用词提示 226 处，未逐条处理。
- ❌ §7 Phase 3 验收：6 档视口 × Chromium / WebKit × 浅/深色全矩阵未跑（已跑的见 9.6）。

### 9.5 第一轮审阅问题的处理

1. ✅ `diffHTML` 误判文件头 / `\ No newline` 错位：已修复并补测试（见 9.3）。
2. ✅ `agentExecutionHelp`：确认自 `8ea13b4` 起就无调用方，已删除；`agentInstallSetup` 的恢复保留（仍建议单独提交）。
3. ✅ 无障碍：`aria-pressed`、`aria-valuenow/min/max`、`pointercancel` 已补。
4. ✅ `settings.js`：开发工具入口打开失败时提示「开发工具未能打开，请重试」。
5. ✅（新发现）1366 以下工作台网格换行：已修复（见 9.3）。

### 9.6 验收结果（第二轮）

| 检查 | 结果 |
| --- | --- |
| `node web/mcp-apps/build-room.mjs` + `scripts/build_web_assets.py --check` | ✅ 无过期产物 |
| `scripts/css_override_report.py` | ✅ 未报出冲突候选 |
| `scripts/check-ui-copy.mjs` | ✅ 明确问题 0；提示 226 |
| `test_ui_design_guard.py` / `test_ui_layout_contracts.py` | ✅ 全部指标在基线内 |
| `node --test tests/test_lazy_assets.mjs tests/test_diff_html.mjs` | ✅ 7 项通过 |
| 12 个 `test_ui_*.py` + `test_call_estimate_ui.py` + `test_context_usage_ui.py` | ✅ 258 项通过（修复后重跑原 5 项失败均通过；其中 4 项在第一轮快照上同样失败） |
| 13 个相关浏览器测试文件（审计、设置、Token、引导、连接、VPS、并发总览、CLI 会话、会话编辑器） | 213 项通过，3 项失败，均非本轮引入（见下两行） |
| `test_panel_session_and_editor.py` | ❌ 1 项：状态显示「保存结果待核对」而非「已生效」；第一轮已记录，HEAD 同样失败 |
| `test_connection_setup_integration.py` | ❌ 2 项：本轮已修正其过期的展开步骤（「排查选项」「其他客户端」），但它断言的文案「未知 / 未观测」「不会自动刷新」自 v1.33.3 起已不存在，HEAD 与第一轮快照上同样失败；需按当前文案更新断言 |
| `test_room_permission_bulk_browser.py`、`test_management_ui_unification.py`、`test_runtime_panel_browser.py` | ❔ 本轮未跑（未改动对应代码） |

> 环境说明：在 CodePier 运行时内跑浏览器测试，需要 `TMPDIR=/tmp/...`（默认临时目录建项目会被拒为 `PROTECTED_ROOT`），并设置 `PLAYWRIGHT_BROWSERS_PATH=~/Library/Caches/ms-playwright`。修改 `web/chat/*.js` 或 `web/chat/styles/*.css` 后必须先运行 `node web/mcp-apps/build-room.mjs`（`web/chat.js`、`web/chat.css` 是生成文件），再运行 `scripts/build_web_assets.py`。

### 9.7 下一步

1. 权限面板去掉行内复选框：批量栏改为「全部项目设为 [档位]」，同步重写 `test_room_permission_bulk_browser.py` 与微信向导测试中依赖复选框的步骤。
2. CLI 会话：配置栏胶囊化，`⌘K` 在 chat-mode 统一到命令面板。
3. 工作台 IDE 顶部工具栏整合。
4. 微信向导减步与标识脱敏；按 `check-ui-copy.mjs --audit` 逐条处理 226 处文案提示。
5. 跑完整 Phase 3 视口矩阵后再提交；`agentInstallSetup` 的恢复建议单独提交。

## 10. 继续实施核实（2026-11-14）

本节覆盖第 9 节记录之后的实际工作树状态；历史测试记录保留，不将未运行项写成通过。此次不提交、推送或部署，保留已有 112 项工作树改动。

### 10.1 落地与复核

- 权限面板及 IDE 顶栏在本次接手前已完成第 9.7 节所列重构，未重复覆盖；新增权限回归断言：打开不隐式授权、没有可见成员选择框/全选、批量操作保留初始占位。
- CLI 配置胶囊补齐 CLI、模型、思考强度、目录摘要及完整悬停说明；关闭设置弹层时 inert，保留 Esc、焦点恢复和草稿。按钮和 Cmd/Ctrl+K 统一命令入口；聊天内同时提供会话命令与全局页面，再次快捷键关闭。独立聊天环境保留 fallback。
- 工作台目录面包屑可点击并带累计目录路径；只导航文件树，不丢失未保存源码。文件树修正相对深度计算并增加缩进导线，触控目录按钮至少 44px。
- 微信向导统一三个展示阶段：扫码、确认范围与绑定、发送配对码；优先已有且已验证在线的 dot，不覆盖明确选择，不自动提交。身份标签即使自身为 wxid_/peer_id 仍脱敏，提交继续使用精确 ID 和版本。服务端要求的本人确认必须保留，轮询不会代为确认。
- 原项目保存失败测试的根因是 fixture 缺失根目录 preview/applied 回执，而非生产代码错误。补齐配置 SHA 等真实回执，并断言明确批准后的恢复与新幂等键；没有放松生产安全逻辑。
- 文案审计当前明确问题 0、场景提示 122（不再是旧记录的 226）。提示集中于恢复/幂等操作、诊断、权限范围及 OIDC 群组映射，不做全局机械替换；必要安全说明保留。

### 10.2 本次验证

- 主页面矩阵扩展为 Chromium/WebKit × 明暗 × 320/390/768/1024/1366/1440，共 24 组合；覆盖 10 管理路由及原生聊天、无横向溢出、主题一致、草稿往返、无模型提交。连同额外合同检查共 **27 passed**。
- 聊天胶囊/导航/窗口回归 **59 passed**；其中一次并行修改导致清单不匹配，最终构建后单独重跑通过。
- 权限与项目编辑器回归 **35 passed**，包含累计面包屑转义及未保存内容保持。
- 微信后端配对/权限 **23 passed**，向导浏览器 **10 passed**，新增 390px 身份确认 **2 passed**（两种引擎）。
- 设计/布局静态合同 **15 passed**；Node lazy-assets/diff/微信辅助测试 **11 passed**。
- CSS 覆盖比较 `web/styles.css web/workspace.css` 为 **0 candidates**；文案明确问题 **0**；`git diff --check` 通过。
- 管理/运行环境/连接组合回归原 65 passed、4 failed；统一「访问配置」页面标题，并修正测试中紧凑导航名称与完整标题、账号头像入口的过期假设后，4 个十九路由/主题/尺寸案例重跑通过，最终该组 **69 项通过**。
- 扩展 UI 外观/会话 Token/层级/重构/触控标准及调用估算/上下文用量 **155 passed**；包括实际浏览器验证，不仅静态合同。
- 最终 assets `--check` 无过期文件，文案检查和 `git diff --check` 通过。本次各独立组共 **406 项通过**（不把环境失败或重跑重复计入）。没有声称覆盖 Windows、Firefox、生产发布或所有真实账号数据。

运行环境注意：CodePier runtime 的 HOME 是隔离目录，不能将 `$HOME/Library/Caches/ms-playwright` 当作已安装缓存。应核实实际已有缓存并显式设置绝对路径；本次使用现有缓存，不安装依赖。所有 Hub/Agent 测试在 /tmp 隔离目录，未触及生产数据。初次缺少浏览器路径及并行改动期间的清单失败日志保留，最终结果以重建后验证为准。

## 11. 未提交改动深度审查（2026-11-14）

按核心面板/弹窗、聊天、房间/微信/权限、安装/连接/更新、管理业务、样式/生成物/CI 分域审查整个 dirty diff，包含接手前的改动，保留用户工作。没有提交、推送、部署、安装依赖或操作生产数据。

### 11.1 已修复的问题

1. `core/ui.mjs`：异步关闭旧弹窗后可能误关随后创建的新弹窗；关闭前重新核对捕获的弹窗身份，并仅关闭该实例。
2. `app.js`：活动徽标初始化计数与运行事件重复计数，终态后残留；改为已知执行者集合与未知余量分离，使用服务端快照校准，按会话/空间/版本隔离异步响应，切换空间清空旧状态。
3. `chat/settings.js`：设置收起时 `/model` 的锚点隐藏且 inert；先展开设置再打开模型选择器，恢复定位与 Esc 焦点。
4. 房间 responsive 样式：长工作标题把移动端操作挤至 39px；固定动作区，标题可缩略，触控目标至少 44px。
5. `panel-update.js`：刷新确认跨账号/空间可能丢弃新草稿；更新/回滚确认也补齐会话、空间、generation 与最新访问状态校验。
6. `call-log.js`：高级筛选启用时，刷新会违背用户的收起选择；保留显式展开/收起状态。
7. 主面板及扩展输入框新边框仅约 1.67:1；仅调整控件边界专用 token，恢复至少 3:1，其余视觉配色保留。
8. 扩展通用状态区域在离线/读取状态时仍显示绿色成功点；改为中性提示，不虚构连接成功。

### 11.2 验证与范围

- 核心弹窗/编辑器：17 passed，含先失败后修复的替换弹窗竞态及活动计数/旧空间响应回归。
- 聊天/Claude/移动端/目录/导航：160 passed，含 Chromium/WebKit、390/1440 的模型选择器回归。
- 安装/目录授权/更新/运行环境/连接/文件入站：277 passed，含 8 个异步确认隔离回归。
- 全部 `test_ui_*.py`：263 passed，包含两引擎、明暗、六尺寸的矩阵及控制边界对比度。
- 管理路由与站点：37 passed。
- 管理业务扩展：291 个唯一用例最终均通过。首次 288 passed、3 个 Token 旧文案/标记断言失败；更新等价断言并保留/加强“假设缓存比例、真实命中未知、供应商记录为准”后，完整 Token 套件 60 passed。不是同一次全量重跑，不重复累计重跑用例。
- 调用记录：38 passed；Node diff 与微信辅助函数：9 passed。
- core 内存重新构建与 bundle 字节一致；MCP App CSS/HTML/manifest、扩展 ZIP 全部 8 项与源码/清单一致；资产 `--check` 无过期项。CSS 覆盖候选 0，明确文案问题 0，`git diff --check` 通过。

房间组合首次 40 passed、1 个 WebKit 控件等待超时及 1 个浏览器 teardown 错误；单独重跑该用例通过，随后完整四文件组合再次运行 **41 passed，exit 0**，期间未修改源码。保留首次不稳定日志，不将其抹去；本次未稳定复现，不能断言已证明所有并行环境问题消失。

上述互不重复的最终验证范围共 **1,133 项通过**（包含 9 项 Node 测试）；不加计前一实施轮的 406 项，不加计重跑或重复的 28 项样式检查。此数量来自各域组合与失败项复测后的唯一用例汇总，并非一次执行全仓库测试。

### 11.3 保留事项

`tests/test_zz_*` 为原有临时探针/截图脚本，`docs/_section9.tmp.md` 含占位符及旧环境说明；此次未擅自删除，不作为正式验收证据。提交前应明确是否纳入。测试未覆盖 Firefox、Windows、真实外部账号数据或生产部署，不保证不存在所有潜在问题。

## 12. 平台缺口与生产只读补验（2026-11-14）

### 12.1 Firefox 与 Windows

- Firefox 尚不能实测。现有 Playwright 1.64.1 要求 Firefox 157.0/revision 1555；Mac 项目缓存只有 Chromium/WebKit，隔离 HOME、常规应用目录和云端 Linux 均未发现可用 Firefox。实际 `firefox.launch()` 返回 executable missing。没有安装依赖或用别的引擎冒充；需先提供匹配运行时。
- Windows 尚不能实测。环境列表只有云端 Linux；CodePier 完整两个远程目标实际为 Darwin arm64 和 Linux aarch64，没有 Windows。项目执行机是 macOS 26.3.1 arm64/Python 3.13.12。
- 在 macOS 补跑 Windows 命令、junction、卸载、任务服务、dot 环境、配置锁六套兼容/模拟测试：**162 passed，exit 0**。这不是 Windows 实机或 Windows 浏览器验证。
- CI 配置有 full 模式的 windows-2025/Python 3.13 核心子集，以及真实 PowerShell 5.1/Task Scheduler 验收；本轮没有触发 CI，不将配置存在当运行通过。仍需真实 Windows 隔离执行环境。

### 12.2 生产安全只读检查

已对既有部署进行只读核实（公开版本省略实际服务地址及精确巡检时间）；全程仅无凭据 GET，不登录、不运行业务变更，不推送或部署。

- `/healthz` 返回 200，已核对部署版本及健康字段，未发现维护状态或写入错误（公开版本省略具体部署版本及运行字段值）。
- 首页 200，标题 CodePier。引用的 52 个同源静态资源全部 200，SHA256 与页面 h 参数全部一致。
- 35 个生产资源与当前工作树不同。本轮修复仍未部署，检查仅支持既有部署的公开健康与静态完整性，不支持新代码已上线或生产业务流程已验收的结论。

### 12.3 房间 WebKit 稳定性补验

原失败权限用例在 8 个独立 WebKit 进程中均通过。新增受控延迟 access_changed SSE 的 Chromium/WebKit 诊断回归亦通过，不能将未证实的 SSE 时序猜测写成根因。

- 完整 43 例组合第一轮通过，第二轮 43 passed 加 1 个 teardown error，确定复现 `Response has been disposed`。原日志显示工作恢复测试 `route.fetch()`/`response.json()` 尚未结束即关闭 context；这是测试资源生命周期问题，不是应用断言失败。
- 仅在 `tests/test_room_work_recovery_browser.py` 的清理阶段先执行 `page.unroute_all(behavior='wait')` 再关闭 context，等待在途路由处理，不吞异常，不放宽权限和撤权逻辑。
- 修复后原 teardown 用例独立 WebKit **5/5 轮通过**，再以显式 `--chat-browser-reuse` 运行两轮完整组合，分别 **43 passed / 43 passed，exit 0**。普通 fixture 默认每测试独立浏览器，不将普通组合冒称共享浏览器验证。
- 延迟 SSE 回归进一步要求实际应用至少两次 `access_changed`，而非仅收到事件；加强断言后两引擎 race + work recovery 共 **4 passed**。
- 权限面板控件等待超时未再次复现，仍未找到确定根因，不能写成已修复；安全清空行为未关闭。重复轮次作为稳定性证据，不累计成唯一测试用例数。
