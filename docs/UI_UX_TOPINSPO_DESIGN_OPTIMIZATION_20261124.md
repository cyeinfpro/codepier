# CodePier UI/UX 设计优化方案（基于 TopInspo Design Vocabulary 体系）

> **状态**：历史设计提案。以下波次、示例和验证目标不代表全部已实现或通过验收；实际已发布行为以相应版本说明为准。
> **文档日期**：2026-11-24\
> **参考来源**：[TopInspo Design Vocabulary — Easing](https://www.topinspo.design/vocabulary?term=easing) 及全站 65 项设计词条体系（[TopInspo Design Vocabulary](https://www.topinspo.design/vocabulary)）\
> **适用基线**：CodePier `v1.34.4` 零外部依赖原生 Web 前端架构（`web/tokens.css`、`web/styles.css`、`web/chat/styles/*.css`、`web/computer.css`、`web/collaboration-room/styles/*.css`、`web/call-log.css`、`web/workspace.css`、`web/vps-terminal/styles.css`）\
> **设计前提**：本方案完全继承并遵守 `docs/UI_UX_SYSTEM_REDESIGN_2026-11-14.md`、`docs/UI_UX_ACTION_LANGUAGE.md` 与 `docs/MOBILE_TERMINAL_FIRST_PRINCIPLES_20261124.md` 既有合同——**不引入外部前端框架，不弱化交互控件边框对比度，不模糊操作语义与恢复边界，不破坏移动端触控与无障碍门禁**。

---

## 1. 背景与目标

CodePier 在前期多轮重整中已经完成了暖灰编辑风底色（`#f5f5f4` / `#141413`）、共享 Token 抽离（`web/tokens.css`）、21 路由移动端触控适配及 Web SSH 终端重构。但在高频的日常研发与 AI 协作场景中，界面仍存在明显的**“工程可用但手感偏硬（Stiff）”**与**“局部几何与层次割裂”**问题。

通过对 [TopInspo Design Vocabulary](https://www.topinspo.design/vocabulary?term=easing) 的系统学习，我们将该体系中经过工程验证的 **7 大界面痛点视角**与 **65 项原子设计词汇**（尤其是动效曲线、同心圆角、边缘渐隐、中段截断与 11 项 AI UI 专项模式）映射到 CodePier 真实代码库，形成一份**可直接按波次编码落地**的 UI/UX 升级设计规范。

### 核心设计原则（不变量约束）

1. **动效服务于空间因果，而非装饰**：进入视图的元素快进缓停，离开视图的元素干脆退场；所有动效受 `prefers-reduced-motion: reduce` 严格门控。
2. **结构线条克制，交互边界清晰**：容器分割采用低噪极细边框（`--ui-line`），但输入框、下拉框、操作按钮必须保留可辨识的 `--ui-control-border`（严禁回退 `docs/UI_UX_SYSTEM_REDESIGN_2026-11-14.md` §3 的对比度红线）。
3. **几何严格同心，层级可计算**：嵌套卡片、弹窗、审批框的内外圆角与内边距遵循同心圆角公式，消除“圆角外大内肥”的视觉毛刺。
4. **AI 交互诚实且低焦虑**：沿用 `docs/UI_UX_ACTION_LANGUAGE.md` 的动作词典，在 `Approval Gate`、`Tool Call Steps`、`Diff View` 与 `Undo Toast` 中强化状态分层与可逆性，绝不用美化遮蔽真实执行状态。

---

## 2. TopInspo Design Vocabulary 体系拆解

### 2.1 以 `Easing` 为核心的动效第一性原理

[TopInspo Design Vocabulary — Easing](https://www.topinspo.design/vocabulary?term=easing) 将 `Easing` 归类于 **`Stiff`（生硬感）** 痛点标签下，给出了一句高度凝练的工程定义：

> *The speed curve of a motion. Things entering should start fast and settle gently.*（运动的速率曲线：进入视图的元素应快速启动，然后轻柔落定。）

页面通过 `Panel glide`（带 `✓ Draft saved` 胶囊的面板滑块）在 `440ms · same distance` 条件下直观对比了两种曲线：

| 对比模式 | 曲线参数 | 视觉与体感特征 | 适用边界 |
| :--- | :--- | :--- | :--- |
| **`Ease-out`（推荐）** | `cubic-bezier(0.32, 0.72, 0, 1)` | 初段响应迅捷、末段平滑减速停靠，贴合真实物理阻尼 | 面板滑入、抽屉展开、弹窗出现、Toast 浮现、Tab 滑块切换 |
| **`Linear`（反例警示）** | `linear` | 匀速推进并在终点生硬急停（`linear · abrupt stop`），页面以红色虚线框警示 | **禁止**用于空间位移与层叠转场；仅限连续循环旋转（Spinner）或确定性倒计时进度条 |

其提供的工程化提示词模板（`Say to AI`）直接给出了可复用的生产级参数：
> `Animate the panel with ease-out, cubic-bezier(0.32, 0.72, 0, 1), over 240ms instead of linear for a soft stop.`

### 2.2 全站 7 大痛点标签与 65 项词条映射

[TopInspo Design Vocabulary](https://www.topinspo.design/vocabulary) 顶部通过 `Jump to` 按**界面诊断症状**（`New`、`Cramped`、`No focus`、`Muddy`、`Stiff`、`UI patterns`、`AI UI`）索引全部 65 个词条，按设计维度归纳如下：

| 设计维度 | 核心词条（65 项全覆盖） | 与 CodePier 的高价值映射场景 |
| :--- | :--- | :--- |
| **1. 间距与布局** | `Padding`、`Gap`、`Line Height`、`Proximity`、`Whitespace`、`Divider vs. Space` | 总览页卡片密度、设置项分组、减少冗余分割线改用留白表达归属 |
| **2. 排版与文字细节** | `Hierarchy`、`Font Weight`、`Tracking`、`Tabular Numbers`、`Text Balance`、`Line Clamp`、`Baseline Alignment`、`Shortcut Hint`、`Middle Truncation` | 文件路径/仓库路径中段截断、全局快捷键 `<kbd>` 提示、数据表与耗时等宽数字 |
| **3. 色彩与对比度** | `Accent`、`Contrast`、`Muted Text`、`Semantic Color`、`Tint & Shade` | 暗色模式层次补齐、语义状态色与文本可读性边界 |
| **4. 表面、边框与空间层次** | `Border Radius`、`Concentric Radius`、`Nested Cards`、`Hairline Border`、`Optical Alignment`、`Avatar Stack`、`Layered Shadow`、`Scrim`、`Edge Fade`、`Sticky Header`、`Floating Bar` | 审批弹窗/项目选择器的同心圆角、三层阴影、垂直长列表边缘渐隐、粘性表头与 Diff 文件头 |
| **5. 动效与微交互** | `Easing`、`Directional Motion`、`Spring`、`Stagger`、`Press State`、`Shared Element`、`Origin-Aware Motion`、`Hover Reveal` | 统一全站缓动曲线、弹窗/抽屉/Toast 方向动效、折叠箭头过渡、列表行悬停显隐 |
| **6. 交互与 UI 模式** | `Button Hierarchy`、`Touch Target`、`Empty State`、`Inline Validation`、`Skeleton`、`Popover vs. Tooltip`、`Modal vs. Drawer`、`Tabs vs. Segmented`、`Switch vs. Checkbox`、`Command Palette`、`Bento Grid`、`Progressive Disclosure`、`Combobox`、`Input Affix`、`Split Button` | 页面 Tab 与分段控制器解耦、结构化骨架屏、搜索框前后缀、复合操作按钮 |
| **7. AI 界面专项模式** | `Prompt Composer`、`Streaming Text`、`Thinking Disclosure`、`Citation Pill`、`Slash Command`、`Diff View`、`Undo Toast`、`Suggested Prompts`、`Tool Call Steps`、`Context Chips`、`Approval Gate` | 原生 Chat 工作台、协作房间、主机/计算机审批收件箱（`#computer-approval-inbox`）、变更审查 Diff 卡片 |

### 2.3 TopInspo 网站自身的 5 项设计工程范式

观察 [TopInspo Design Vocabulary](https://www.topinspo.design/vocabulary?term=easing) 站点本身的交互实现，以下 5 点与 CodePier 的编辑风工作台气质高度契合，值得直接吸收：

1. **字体功能三分法**：标题与主视觉锚点强调辨识度，正文保持高可读无衬线，技术参数/代码/数值（如 `cubic-bezier(0.32, 0.72, 0, 1)`、`440ms`）严格使用等宽字体。
2. **极简中性底色 + 功能性语义点亮**：常态界面以低饱和暖灰/纯白与极细边框维持安静，仅在警示或核心焦点处使用高辨识语义色。
3. **垂直列表动态 `Edge Fade`**：左侧 65 词条滚动列表在上下边缘使用平滑透明遮罩暗示可滚动，滚动到顶/底时对应侧渐隐自动消退，既克制又无须依赖厚重滚动条。
4. **搜索框内嵌 `Shortcut Hint`**：`Filter terms...` 输入框右侧常驻 `/` 键盘提示徽章，降低快捷键发现成本。
5. **交互式反馈闭环**：分段控件切换与重播操作响应干脆，状态改变均有清晰的视觉落点。

---

## 3. CodePier 现有 UI/UX 源码级诊断矩阵

基于对 `web/` 目录下 49 份样式文件与核心页面模板的逐项检索，CodePier 当前存在以下 **8 类明确的源码级短板**：

| 编号 | 痛点标签 | 源码位置与实测证据 | 体验问题 |
| :--- | :--- | :--- | :--- |
| **GAP-01** | `Stiff` | `web/tokens.css:116-118` 仅定义 `--ui-motion-fast: 120ms`、`--ui-motion-normal: 180ms`、`--ui-ease: cubic-bezier(0.2, 0.8, 0.2, 1)`；`web/call-log.css:227,325,614,859`、`web/workspace.css:1451`、`web/queue.css:229`、`web/mcp-apps/app.css:22` 绕过 Token 使用裸 `ease` 或无曲线参数 | 动效节奏全站不统一；缺少专门的进场缓停曲线（`cubic-bezier(0.32, 0.72, 0, 1)`）、退场加速曲线与轻弹簧曲线 |
| **GAP-02** | `Stiff` | `web/styles.css:4552-4567` 的 `@keyframes page-in`（`opacity: 0.6 -> 1`）、`@keyframes dialog-in`（`opacity: 0 -> 1`）与 `web/collaboration-room/styles/diagnostics.css:188` 的 `@keyframes cr-panel-in` 均为纯透明度淡入；`.toast`（`web/styles.css:3057`）复用 `dialog-in`；`web/chat/styles/execution.css:20-32` 折叠箭头旋转无 `transition`；全站无 `@starting-style` 或退出过渡 | 弹窗、Toast、协作抽屉与折叠面板显得“闪现/闪烁”，缺乏空间方向感（`Directional Motion`）与物理连续性 |
| **GAP-03** | `Muddy` / `Cramped` | `web/computer.css:89-160` `#computer-approval-inbox` 外圆角 `20px` + `padding: 10px`，内部 `> section` 与 `button` 仍用 `20px`；`web/chat/styles/execution.css:93-120` 审批卡外圆角 `20px` + `padding: 18px 20px`，内 `.chat-approval-kv` 用 `12px`；`web/chat/styles/modal.css:2-75` 外 `20px` + `padding: 24px`，内选项 `12px` | 违反 `Concentric Radius` 几何规律，嵌套容器拐角处间隙忽宽忽窄，视觉显得粗糙拥挤 |
| **GAP-04** | `Muddy` | `web/tokens.css:188-221` 暗色模式覆写了 `--ui-elev-1` 与 `--ui-shadow`（`--ui-elev-3`），但**漏掉了 `--ui-elev-2` 的暗色覆写**（仍继承浅色 `rgb(0 0 0 / 8%)`）；浮层卡片在暗色模式下缺少顶部 `1px` 内高光（specular hairline） | 暗色主题下中层浮层（下拉菜单、浮窗、卡片悬浮态）与深灰底色（`#141413` / `#1c1c1b`）层次黏连 |
| **GAP-05** | `No focus` | 全站 49 份 CSS 中 `mask-image` 仅出现于 `web/styles.css:3969,4304` 两处横向渐变；侧边栏 `.nav`（`web/styles.css:250`）、`#chat-project-results`（`web/chat/styles/modal.css:64`）、`#computer-approval-inbox`（`web/computer.css:89`）、`.modal-body`（`web/styles.css:1633`）均无垂直 `Edge Fade` | 长列表底部元素被容器硬切（hard clip），在隐藏/细滚动条环境下缺乏“下方仍有内容”的视觉暗示 |
| **GAP-06** | `No focus` | `web/chat/styles/modal.css:101` `.chat-project-path` 与多处文件/调用日志路径使用末端截断 `text-overflow: ellipsis`；搜索框与弹窗普遍缺少 `<kbd>` 快捷键提示（`Shortcut Hint`） | 长路径末端截断直接裁掉最关键的仓库名/文件名；键盘高频用户难以发现 `/`、`⌘K`、`Esc`、`⌘↵` 等效率入口 |
| **GAP-07** | `UI patterns` | `web/styles.css:1250-1285` 将 `.tabs`、`.integration-tabs`、`.settings-groups` 全部统一成同一种灰底胶囊容器；`web/styles.css:3108` `.skeleton` 是高度 `220px` 的单一灰块脉冲 | 页面级导航 Tab 与视图切换 Segmented Control 视觉角色混同；加载态骨架屏与真实内容结构脱节，切换时易产生布局跳动（CLS） |
| **GAP-08** | `AI UI` | `web/chat/styles/execution.css` 的工具调用步骤与思考过程缺乏时间轴状态节点与流式退避节奏；`web/styles.css:3034` `#toasts` 不支持带动作/倒计时的 `Undo Toast`；`web/chat/styles/review.css` Diff 文件头缺少吸顶与长路径中段截断 | AI 长链路执行时用户难以一眼区分“思考中 / 工具执行中 / 等待审批 / 需核实结果”，审查大 Diff 时丢失当前文件上下文 |

---

## 4. 六大核心优化专题与工程规范

### 专题 A：动效系统升级（解决 `Stiff` 痛点）

**对应词条**：`Easing`、`Directional Motion`、`Spring`、`Stagger`、`Press State`、`Origin-Aware Motion`、`Hover Reveal`

#### A.1 扩展 `web/tokens.css` 动效 Token 矩阵

将单一 `--ui-ease` 升级为语义分层的曲线与时长系统，引入 [TopInspo Easing](https://www.topinspo.design/vocabulary?term=easing) 推荐的 `cubic-bezier(0.32, 0.72, 0, 1)` 作为空间面板主曲线：

```css
/* web/tokens.css — Motion & Easing System */
:root {
  /* 时长阶梯（Duration Scale） */
  --ui-motion-press: 80ms;    /* 按压回弹、复选框微反馈 */
  --ui-motion-fast: 140ms;    /* 按钮 Hover、颜色/边框切换、箭头旋转 */
  --ui-motion-normal: 200ms;  /* Popover、Tooltip、Tab 滑块、Inline 展开 */
  --ui-motion-panel: 240ms;   /* 弹窗、抽屉、审批浮层、Toast 进场（TopInspo 推荐基准） */
  --ui-motion-exit: 150ms;    /* 退场永远比进场更快，保持干脆感 */

  /* 语义曲线（Easing Curves） */
  --ui-ease-out: cubic-bezier(0.32, 0.72, 0, 1);      /* 核心进场与位移：快进缓停（Panel glide） */
  --ui-ease-in: cubic-bezier(0.4, 0, 1, 1);           /* 退场加速消失 */
  --ui-ease-in-out: cubic-bezier(0.4, 0, 0.2, 1);     /* 同屏状态切换（Segmented 滑块） */
  --ui-ease-spring: cubic-bezier(0.22, 1.12, 0.36, 1);/* 极轻度弹性（仅用于开关/徽章/成功确认，位移 <= 4px） */
  --ui-ease: var(--ui-ease-out);                      /* 向下兼容既有引用 */
}
```

#### A.2 统一空间方向动效（`Directional Motion` & `Origin-Aware Motion`）

1. **Modal 弹窗（`web/styles.css:1585-1610` & `web/chat/styles/modal.css`）**：
   - 将 `@keyframes dialog-in` 从纯 `opacity: 0 -> 1` 升级为 `opacity: 0; transform: translateY(8px) scale(0.985)` → `opacity: 1; transform: none`，使用 `var(--ui-motion-panel) var(--ui-ease-out)`。
   - 配套 `.modal-backdrop` 与 `dialog::backdrop` 使用 `160ms var(--ui-ease-out)` 淡入。
   - 移动端（`<= 640px`）底部抽屉（Bottom Sheet，如 OAuth 授权 Sheet、移动导航 Sheet）使用 `translateY(100%) -> translateY(0)` 配合 `var(--ui-motion-panel) var(--ui-ease-out)`。
2. **Toast 通知（`web/styles.css:3034-3060`）**：
   - 独立的 `@keyframes toast-in`：`from { opacity: 0; transform: translateY(10px) scale(0.98); }`，从右下角视口边缘轻柔升起；关闭时增加 `.toast[data-leaving="true"]` 退场动画（`var(--ui-motion-exit) var(--ui-ease-in)`）。
3. **Popover / 下拉菜单（`.tool-menu-items`、`.chat-popover`）**：
   - 依据弹出方向设置 `transform-origin`（向下弹出设为 `top right` / `top left`，向上弹出设为 `bottom right`），实现 `Origin-Aware Motion`（`scale(0.96) translateY(-4px) -> none`）。
4. **折叠箭头与渐进披露（`web/chat/styles/execution.css`、`web/chat/styles/review.css`）**：
   - 为 `.chat-process > summary::before`、`.chat-review > summary::before` 补齐 `transition: transform var(--ui-motion-fast) var(--ui-ease-out)`。
5. **列表交错入场（`Stagger`）**：
   - 仅限首次首屏渲染的轻量列表（如工具调用步骤、快捷指令菜单、空态推荐提示词），每项延迟 `calc(min(var(--stagger-i, 0), 6) * 28ms)`，**硬上限封顶在 6 项（`<= 168ms`）**，防止长列表拖沓。
6. **清理全站离散 `ease`**：
   - 统一替换 `web/call-log.css`、`web/workspace.css`、`web/queue.css`、`web/mcp-apps/app.css` 中的裸 `ease` 为 `var(--ui-ease-out)`。

#### A.3 按压反馈与悬停显隐（`Press State` & `Hover Reveal`）

- **`Press State`**：保留 `web/styles.css:4503` 的 `scale(0.98)`，将其 `transition` 时长细化为 `transform var(--ui-motion-press) var(--ui-ease-out)`，松手回弹用 `var(--ui-motion-fast)`，手感更脆。
- **`Hover Reveal`**：表格行尾操作按钮（`call-log.css`、项目列表、会话历史行）在桌面精确指针下默认 `opacity: 0`，于 `@media (hover: hover) and (pointer: fine)` 内的 `:hover` 及全局 `:focus-within` 下平滑显现（`opacity: 1`）；触屏粗指针环境保持常显，确保零可发现性损失。

---

### 专题 B：空间层次与表面几何（解决 `Muddy` / `Cramped` 痛点）

**对应词条**：`Concentric Radius`、`Nested Cards`、`Hairline Border`、`Layered Shadow`、`Scrim`、`Edge Fade`、`Sticky Header`、`Floating Bar`

#### B.1 同心圆角数学合同（`Concentric Radius`）

建立统一的同心圆角公式：`R_inner = max(var(--ui-radius-xs), R_outer - Padding)`，并在 `web/tokens.css` 新增结构化派生变量：

```css
/* web/tokens.css — Concentric Radius Contract */
:root {
  --ui-radius-xs: 6px;
  --ui-radius-small: 8px;
  --ui-radius-md: 12px;
  --ui-radius-lg: 16px;
  --ui-radius-xl: 20px;

  /* 典型容器嵌套速查：外圆角 20px 时 */
  --ui-radius-inner-tight: 10px;   /* 外 20px - 内边距 10px（如 #computer-approval-inbox） */
  --ui-radius-inner-normal: 8px;   /* 外 20px - 内边距 12~16px（如卡片内嵌代码块/KV区） */
  --ui-radius-inner-pill-seg: 9px; /* 外 12px - 内边距 3px（如 Segmented 控件内按钮） */
}
```

**重点修复清单**：
- `web/computer.css:89-165`：`#computer-approval-inbox`（外 `20px`，`padding: 10px`）→ 内部 `> section` 改为 `border-radius: 10px`（原 `20px`），内部警示条与主按钮改为 `8px`（原 `12px`/`20px`）。
- `web/chat/styles/execution.css:93-120`：`.chat-message-approval`（外 `20px`，`padding: 18px 20px`）→ 内部 `.chat-approval-kv` 改为 `8px`（原 `12px`）。
- `web/styles.css:1596-1665`：`.modal`（外 `20px`，`padding: 20px`）→ 内部 `.code-box` 改为 `10px`（原 `16px`）。
- `web/chat/styles/modal.css:2-75`：`.chat-project-sheet`（外 `20px`，`padding: 20px`）→ 内部 `.chat-project-option` 改为 `10px`（原 `12px`），列表容器圆角对齐。

#### B.2 三层阴影体系与暗色模式补齐（`Layered Shadow` & `Hairline Border`）

修复 `web/tokens.css` 暗色模式漏覆写 `--ui-elev-2` 的问题，并采用“接触阴影 + 环境弥散 + 极细外描边（浅色）/ 顶部 1px 内高光（暗色）”的三层模型：

```css
/* Light Mode */
:root {
  --ui-elev-1: 0 1px 2px rgb(0 0 0 / 4%), 0 0 0 1px rgb(0 0 0 / 3%);
  --ui-elev-2: 0 4px 12px rgb(0 0 0 / 6%), 0 1px 3px rgb(0 0 0 / 4%), 0 0 0 1px rgb(0 0 0 / 4%);
  --ui-elev-3: 0 16px 40px rgb(0 0 0 / 11%), 0 4px 12px rgb(0 0 0 / 5%), 0 0 0 1px rgb(0 0 0 / 5%);
}

/* Dark Mode（同步写入 html[data-appearance="dark"] 与 @media (prefers-color-scheme: dark)） */
html[data-appearance="dark"] {
  --ui-elev-1: inset 0 1px 0 rgb(255 255 255 / 5%), 0 1px 2px rgb(0 0 0 / 28%);
  --ui-elev-2: inset 0 1px 0 rgb(255 255 255 / 6%), 0 8px 24px rgb(0 0 0 / 34%), 0 2px 6px rgb(0 0 0 / 20%);
  --ui-elev-3: inset 0 1px 0 rgb(255 255 255 / 8%), 0 20px 48px rgb(0 0 0 / 48%), 0 4px 12px rgb(0 0 0 / 28%);
}
```

> **边界护栏**：`Hairline Border`（`--ui-line`）仅用于布局区域分割与静态信息卡片；所有可输入表单项（`input`、`select`、`textarea`）严格保留 `--ui-control-border`，不降低 WCAG 3:1 非文本对比度。

#### B.3 垂直滚动列表边缘渐隐（`Edge Fade`）

借鉴 [TopInspo Design Vocabulary](https://www.topinspo.design/vocabulary?term=easing) 左侧词汇表的优雅做法，为 CodePier 的 5 个高频垂直滚动容器引入可复用的 `.scroll-edge-fade` 能力：
- 目标容器：`.nav` 侧边导航栏（`web/styles.css:250`）、`#chat-project-results`（`web/chat/styles/modal.css:64`）、`#computer-approval-inbox`（`web/computer.css:89`）、`.modal-body`（`web/styles.css:1633`）、会话历史侧栏。
- 实现机制：在 `web/core/ui.mjs` 提供轻量 `bindScrollEdgeFade(el)` 观察器（结合 `scroll` + `ResizeObserver` 更新 `data-fade-top="true|false"` 与 `data-fade-bottom="true|false"`），由 CSS 动态切换 `mask-image`：
  - 仅顶部可滚：`linear-gradient(to bottom, transparent, black 20px)`
  - 仅底部可滚：`linear-gradient(to bottom, black calc(100% - 20px), transparent)`
  - 双向可滚：`linear-gradient(to bottom, transparent, black 20px, black calc(100% - 20px), transparent)`
  - 滚到顶/底时对应侧渐隐完全消失，不遮挡首尾条目文字。

---

### 专题 C：排版精度与信息密度（解决 `No focus` 痛点）

**对应词条**：`Hierarchy`、`Tracking`、`Tabular Numbers`、`Text Balance`、`Line Clamp`、`Baseline Alignment`、`Shortcut Hint`、`Middle Truncation`

1. **长路径中段截断（`Middle Truncation`）**：
   - 在 `web/core/ui.mjs` 新增 `formatMiddlePath(path)` / CSS 双段弹性布局（`.middle-truncate`，前段 `flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis;` + 后段 `flex: 0 0 auto; white-space: nowrap;` 锁定最后 1–2 级目录与文件名）。
   - 覆盖场景：`#chat-project-results .chat-project-path`、Diff 审查文件头（`.chat-review-file > summary > strong`）、工作台面包屑路径、调用日志操作路径，完整路径通过 `title` 与一键复制保留。
2. **快捷键提示徽章（`Shortcut Hint`）**：
   - 在 `web/styles.css` 定义统一 `.shortcut-hint`（复用 `<kbd>` 语义标签，等宽字体 `11px`、`min-width: 20px`、`height: 20px`、`--ui-surface` 底色 + `--ui-border` 细边 + `inset 0 -1px 0 var(--ui-border)` 微立体键帽感），并在 `@media (pointer: coarse)` 触屏端自动隐藏。
   - 落位点：全局快速跳转（`⌘K` / `Ctrl+K`）、列表筛选搜索框（`/`）、聊天发送按钮旁（`⌘↵`）、弹窗关闭按钮（`Esc`）。
3. **标题平衡与多行裁切（`Text Balance` & `Line Clamp`）**：
   - 将 `text-wrap: balance` 从 `h1, h2` 扩展到 `.modal-header h2`、`#computer-approval-inbox h3`、`.empty strong`。
   - 提供标准 `.line-clamp-2` 与 `.line-clamp-3` 工具类用于项目摘要、调用日志简述与空态说明，避免不等高卡片破坏栅格韵律。
4. **数值与字距精校（`Tabular Numbers` & `Tracking`）**：
   - 核查所有动态刷新数值（SSE 心跳倒计时、Token 计数、审批过期倒计时、队列并发数、终端尺寸 `cols×rows`），确保 100% 启用 `font-variant-numeric: tabular-nums` 防抖动。
   - 英文全大写微标签（如 `MCP`、`SSH`、`API`）允许 `letter-spacing: 0.04em`，中文正文与标题保持 `0` 或 `-0.015em ~ -0.025em`（覆写 `web/styles.css:6` 的全局 `* { letter-spacing: 0; }` 对小号英文徽章的压平效应）。

---

### 专题 D：交互模式与控件语义（`UI patterns` 规范化）

**对应词条**：`Tabs vs. Segmented`、`Button Hierarchy`、`Skeleton`、`Input Affix`、`Split Button`、`Modal vs. Drawer`、`Popover vs. Tooltip`、`Empty State`、`Inline Validation`

| 模式对比 / 升级项 | 现状问题 | CodePier 优化规范 |
| :--- | :--- | :--- |
| **`Tabs vs. Segmented`** | `web/styles.css:1250` 将页面级导航 Tab 与局部视图开关混用同一套灰底胶囊样式 | **页面级导航（`.tabs`）**：改用底部 `2px` 强调色指示条（`--ui-accent`），透明背景、底部分割线对齐；**局部模式切换（`.segmented`，如原 `.settings-groups`、Diff 统一/折行视图）**：保留紧凑轨道 + 同心圆角白底滑块（`--ui-radius-inner-pill-seg`）与 `var(--ui-motion-normal) var(--ui-ease-out)` 平滑过渡 |
| **`Skeleton`** | `web/styles.css:3108` 单一 `220px` 灰色脉冲块，与页面实际骨架不符 | 按路由拆分 3 种结构化骨架模板：`.skeleton-card-grid`（总览/VPS 卡片）、`.skeleton-table-rows`（调用日志/队列行）、`.skeleton-chat-thread`（聊天气泡与工具栏），采用低对比度横向微流光（`@keyframes skeleton-shimmer`，`prefers-reduced-motion` 下退化为静态占位） |
| **`Input Affix`** | 搜索输入框缺少前置语义图标与后置清除/`/` 提示 | 引入 `.input-affix` 容器：左侧 `16px` 图标（如放大镜），右侧放置清除按钮（有值时显现）或 `.shortcut-hint`（空值且精确指针时显现），严格保证移动端输入 `font-size: 16px` 防 iOS 缩放 |
| **`Split Button`** | 主操作与次级恢复/变体动作并排堆叠，移动端易换行拥挤 | 在明确安全的同源动作组（如“发送”+“发送并新建会话” / “复制命令”+“复制 PowerShell 版本”）引入 `.split-btn`；**严禁**将高风险副作用动作（如“重新执行”“取消执行”）藏在只读恢复按钮（“查看原结果”）的下拉菜单里 |
| **`Modal vs. Drawer`** | 部分轻量详情查看与多步表单都堆在居中 Modal 中 | **决策/高风险确认/短表单** → 居中 `Modal`；**上下文检视/日志详情/长配置编辑** → 右侧 `Drawer`（桌面端 `420px–560px` 滑入，移动端 `<= 640px` 自动转为底部 `Bottom Sheet`），均采用 `var(--ui-motion-panel) var(--ui-ease-out)` |
| **`Popover vs. Tooltip`** | 部分说明既依赖原生 `title`（触屏不可见）又在卡片内堆文字 | **纯短标签/快捷键提示（<= 6 字、无交互）** → `Tooltip`（桌面 hover 延迟 `300ms`）；**含说明/链接/禁用原因/操作的浮层** → 点击触发 `Popover`，确保 `docs/UI_UX_ACTION_LANGUAGE.md` 中“禁用原因界面可读”在触屏端同样成立 |

---

### 专题 E：AI 工作台专项体验重构（映射 TopInspo 11 项 `AI UI` 词条）

CodePier 本质上是 **AI Coding / MCP / Agent 编排工作台**，TopInspo 词汇表中最前瞻的 11 项 `AI UI` 词条可直接一一落位到 CodePier 核心界面：

| TopInspo AI UI 词条 | CodePier 对应模块 | 具体优化设计 |
| :--- | :--- | :--- |
| **`Prompt Composer`** | `web/chat/styles/composer.css` | 聚焦时使用平滑边框与柔和光环过渡（`var(--ui-motion-fast) var(--ui-ease-out)`）；底部栏清晰区分左（附件/上下文/模型配置）与右（`Shortcut Hint` + 发送/停止按钮）；发送按钮就绪态与禁用态对比更明确 |
| **`Context Chips`** | `web/chat/styles/composer.css`、`web/chat/uploads.js` | 已选项目、工作树、上传附件、引用文件统一渲染为可移除胶囊（`.context-chip`），带文件类型微图标、路径 `Middle Truncation` 与独立 `×` 触控热区 |
| **`Suggested Prompts`** | `web/chat/` 空会话态（`.chat-starters`） | 空会话页提供 3–4 个按当前项目状态动态生成的行动建议卡片，支持轻量 `Stagger` 入场与明确点击反馈 |
| **`Slash Command`** | `web/chat/` 输入区 & 协作房间 | 输入 `/` 时弹出 `Origin-Aware` 浮层命令菜单，支持键盘上下选择、匹配高亮与右侧 `Shortcut Hint` / 一句话说明 |
| **`Thinking Disclosure`** | `web/chat/styles/execution.css`（`.chat-message-reasoning`） | 思考中展示低饱和流光标题（“正在推理 · `tabular-nums` 耗时”），完成后默认折叠为单行摘要，点击平滑旋转箭头展开，不挤占最终正文视觉主角地位 |
| **`Tool Call Steps`** | `web/chat/styles/execution.css`（`.chat-process`） | 将嵌套工具消息升级为清晰的垂直时间轴（`1px` 轨道 + 状态节点：执行中脉冲点 / 成功绿点 / 失败红点 / 待核实琥珀点），单行展示“工具名 + 关键目标（`Middle Truncation`）+ 耗时（`tabular-nums`）”，展开查看入参与回执 |
| **`Streaming Text`** | `web/chat/styles/conversation.css` | 流式输出尾部增加柔和的呼吸光标或尾部淡入效果，代码块与表格在流式增量渲染时锁定最小高度与水平滚动，消除页面抖动 |
| **`Citation Pill`** | `web/call-log.css`（`.call-id-chip`）、`web/chat/` | 将原操作 ID（`operation_id`）、代码文件路径行号、文档引用统一为紧凑 `.citation-pill`（等宽字体 + 微底色 + 点击跳转/复制成功态轻弹簧反馈） |
| **`Diff View`** | `web/chat/styles/review.css`、`web/mcp-apps/changes-v1.html` | 增删行采用低噪语义色带（`color-mix` 混合 `--ui-success`/`--ui-danger` `10%–14%` 透明底）+ 左侧 `2px` 语义色条；文件头启用 `position: sticky; top: 0`（`Sticky Header`）并应用 `Middle Truncation`；代码横向溢出时右侧加 `Edge Fade` |
| **`Approval Gate`** | `web/computer.css`（`#computer-approval-inbox`）& `web/chat/styles/execution.css`（`.chat-message-approval`） | 1）严格修复同心圆角（外 `20px` / 节 `10px` / 键值区与按钮 `8px`）；2）进场采用 `translateY(12px)` + `240ms cubic-bezier(0.32, 0.72, 0, 1)`；3）信息层次分为“主体与范围（Sticky 徽章）→ 拟执行动作摘要 → 风险/后果提示条 → 主次分明的批准/拒绝按钮”；4）过期或不可用态明确区分，不误导重放 |
| **`Undo Toast`** | `web/styles.css:3034`（`#toasts`）& `web/ui.js` | 扩展 `showToast(msg, { actionLabel, onAction, duration, tone })`：支持右侧内联操作按钮（如“撤销”“查看原结果”）及底部 `2px` 线性倒计时条（此处合理使用 `linear` 映射真实剩余时间），悬停或聚焦时暂停倒计时 |

---

### 专题 F：无障碍、触控与语义护栏

所有优化必须通过以下硬性门禁：

1. **减动效完全兼容**：在 `@media (prefers-reduced-motion: reduce)` 下，所有位移、缩放、交错延迟与骨架流光全部归零，仅保留必要的瞬时状态切换（`opacity` 或无动画直接呈现）。
2. **指针能力严格隔离**：`Hover Reveal`、`Shortcut Hint` 仅在 `@media (hover: hover) and (pointer: fine)` 下启用；粗指针（`pointer: coarse`）保持常显、触控热区 `>= 44px`、表单字号 `>= 16px`。
3. **对比度与强高对比模式**：在 `@media (prefers-contrast: more)` 与 `@media (forced-colors: active)` 下，同心圆角与 `Edge Fade` 不得削弱边框可见性，禁用 `mask-image` 防止高对比模式下裁切文本。
4. **动作词典一致性**：`Undo Toast`、`Split Button`、`Approval Gate`、`Tool Call Steps` 的所有可见文案须通过 `scripts/check-ui-copy.mjs` 与 `docs/UI_UX_ACTION_LANGUAGE.md` 核验。

---

## 5. 分波次实施路线图与验收标准

为保持零外部依赖与低回归风险，建议分 **3 个波次**小步合入，每波次独立运行资产构建（`python3 scripts/build_web_assets.py`）与回归测试：

### Wave 1：设计系统底座与动效/几何纠偏（低风险、全局收益最高）

- **涉及文件**：`web/tokens.css`、`web/styles.css`、`web/computer.css`、`web/chat/styles/{execution,modal,review}.css`、`web/call-log.css`、`web/workspace.css`、`web/queue.css`、`web/mcp-apps/app.css`
- **核心改动**：
  1. 落地新版动效 Token（`--ui-ease-out: cubic-bezier(0.32, 0.72, 0, 1)`、`--ui-motion-panel: 240ms` 等）并统一替换全站离散 `ease`。
  2. 升级 `dialog-in`、`toast-in`、`cr-panel-in` 方向动效，补齐折叠箭头过渡与 Popover `transform-origin`。
  3. 修复暗色模式 `--ui-elev-2` 漏覆写，落地三层阴影与同心圆角修复（重点 `#computer-approval-inbox`、`.chat-message-approval`、`.modal`、`.chat-project-sheet`）。
- **验收标准**：桌面/移动视口下无布局溢出；`prefers-reduced-motion: reduce` 零残留动画；暗色模式所有浮层可见清晰边界与层次。

### Wave 2：排版工具、滚区渐隐与通用交互模式升级

- **涉及文件**：`web/core/ui.mjs`、`web/ui.js`、`web/styles.css`、`web/chat/styles/modal.css`、`web/call-log.css`
- **核心改动**：
  1. 落地 `bindScrollEdgeFade` 垂直边缘渐隐（侧边栏、项目选择器、审批收件箱、弹窗内容区）。
  2. 落地 `Middle Truncation` 路径中段截断组件与 `.shortcut-hint` 键盘徽章（`/`、`⌘K`、`Esc`、`⌘↵`）。
  3. 拆分 `.tabs`（底线导航）与 `.segmented`（滑块开关），升级 `.input-affix` 与 3 类结构化骨架屏。
- **验收标准**：超长路径（> 80 字符）保留末尾文件名可见；滚区首尾渐隐随滚动位置准确启停；触屏端不显示无意义快捷键提示。

### Wave 3：AI 工作台专项体验打磨（11 项 `AI UI` 闭环）

- **涉及文件**：`web/chat/styles/{composer,execution,conversation,review}.css`、`web/chat/{timeline,view,approvals}.js`、`web/mcp-apps/changes-v1.html`、`web/styles.css`、`web/ui.js`
- **核心改动**：
  1. 升级 `Tool Call Steps` 时间轴与 `Thinking Disclosure` 折叠态，补齐耗时 `tabular-nums` 与 6 项封顶 `Stagger`。
  2. 升级 `Diff View` 语义色带、粘性文件头与横向 `Edge Fade`。
  3. 升级 `#toasts` 支持 `Undo Toast`（操作按钮 + 线性倒计时条 + 悬停暂停）与 `Context Chips` / `Citation Pill` 统一样式。
- **验收标准**：通过 `tests/test_mobile_first_principles_gates.py`、`scripts/check-ui-copy.mjs` 与 Playwright 多视口（320 / 390 / 768 / 1440px）亮暗双主题截图核验，且 `changes-v1.html` 在 `310px–345px` 极窄气泡内无横向撑破。

---

## 6. 参考资料

- [TopInspo Design Vocabulary — Easing](https://www.topinspo.design/vocabulary?term=easing)
- [TopInspo Design Vocabulary（全站 65 词条与 7 大痛点标签）](https://www.topinspo.design/vocabulary)
- CodePier 既有规范：`docs/UI_UX_SYSTEM_REDESIGN_2026-11-14.md`、`docs/UI_UX_ACTION_LANGUAGE.md`、`docs/MOBILE_TERMINAL_FIRST_PRINCIPLES_20261124.md`
