# CodePier (`MCP`) 操作审计页（Audit Page）第一性原理深度优化方案（2026-11-18）

> **审计基线**：分支 `main` · HEAD `4cac678d92802e0b302ca495e6a3bfe261ed0b07`
>
> **覆盖链路**：
> - **前端表现与状态机**：`web/call-log.js`（561 行）、`web/call-log.css`（420 行）、`web/queue.js`（434 行）、`web/queue.css`（309 行）、`web/app.js`（路由/SSE/重试/全部事件表格）、`web/core/dom.mjs`（Keyed DOM 调和器）、`web/ui.js`、`web/index.source.html`
> - **后端接口、查询与推送**：`hub/call_log.py`（453 行）、`hub/api/activity.py`（297 行）、`hub/queue_panel.py`（106 行）、`hub/queue_contention.py`（381 行）、`hub/diagnostics.py`（310 行）、`hub/runtime.py`（SSE `watchers` / `queue_annotations` / `operation`）、`hub/store.py`、`hub/iam_schema.py`、`shared/audit_redaction.py`
> - **回归门禁基线**：`tests/test_call_log.py`、`tests/test_call_log_api.py`、`tests/test_call_log_browser.py`、`tests/test_call_log_fullstack.py`

---

## 0. 线上实拍病灶还原与第一性原理建模

### 0.1 线上实拍（`v1.33.23 更新中` / 滚动升级窗口）六大病灶逐帧溯源

结合真实运行界面（`Personal / Legacy / 操作审计`）与全链路源码交叉比对，当前页面在高并发多项目调用与瞬态网络抖动（如 Nginx `HTTP 502` 滚动重启窗口）下暴露出六类耦合缺陷：

1. **上下视图“精神分裂”与“幽灵执行中”冻结（最严重体验缺陷）**
   - **现象**：顶部「当前队列」显示 `运行中: 1`（`Nexus · exec · #0940ffd50dea`），而下方「工具调用流水」不仅**漏掉了正在运行的 `#0940ffd50dea`**，反而挂着三条早已结束或失联的旧记录持续显示 `• 执行中`（`#3152128564fb` 定格 `已历时 10.4 秒`、`#f1457f457fa9` 定格 `已历时 49.1 秒`、`#bc39d68c5b9f` 定格 `已历时 64.3 秒`）。
   - **源码级根因链**：
     1. **严苛的防抖动锁死条件阻止新行入列**：`web/call-log.js:374-381` 规定仅当 `!opened.size && window.scrollY < 60 && !focused?.matches('input,select,textarea,button,summary,pre')` 时才自动刷新列表 DOM。由于顶部「当前队列」本身高达 `~320px`，用户只要轻微滚动超过 `60px` 或点击过任意按钮，新产生的 `#0940ffd50dea` 就会被拒之门外；若随后的轮询恰逢 502 报错，连 `#call-new` 提示条也可能错失更新窗口。
     2. **队列组件与调用列表零状态共享 + 重试策略非对称**：`web/call-log.js:338` 显式传入 `{ retryDelays: [] }` 遇 502 立即失败；而 `web/queue.js:261` 走 `app.js:252` 默认 `retryDelays = [500, 1000, 2000, 4000]`（连续重试 4 次跨越 7.5 秒）。两者在不同时间点采样成功/失败，各自维护孤立 `rows`，队列明明已知旧任务退场，却无法通知下方列表收敛终态。
     3. **SSE 实时载荷被丢弃**：`web/app.js:853` 在收到 `m.type === 'operation'` 时已拿到 `{ id, state }`，用于更新侧栏蓝点 `navActivityOperation(m.data)`，但 `app.js:874-886` 对审计页却**丢弃 `m.data`**，退化为 500ms 后重发整页 HTTP `GET /api/call-log`；一旦该 HTTP 请求撞上瞬态 502，已送达浏览器的终态事件即被永久丢弃。
     4. **无本地单调时钟插值**：`elapsed_ms` 仅由服务端在每次轮询时计算一次（`hub/call_log.py:130`），平时呈 5 秒一跳的顿挫感，断连时直接僵死，且无任何“快照陈旧”视觉降级。

2. **队列区状态自相矛盾（报错文案与未降级旧卡片并存）**
   - **现象**：标题下方显示 `队列暂不可用：请求失败 (HTTP 502)`，紧接着却原样高亮展示旧快照的 `全部活动 1` 与 `#0940ffd50dea` 卡片及「请求取消」按钮。
   - **源码定位**：`web/queue.js:280-287` 在非 `401/403/404` 异常时保留旧 DOM（防闪退初衷正确），但未给 `.queue-panel` 标记 `data-stale="true"`，也未注明“以下为 `HH:MM:SS` 最后同步快照”，导致用户误以为队列既不可用又精确等于 1。

3. **首屏空间严重倒挂（Above-the-Fold 预算失衡）**
   - **现象**：1080p 桌面视口下，页头（`~68px`）+「当前队列」大面板（含 4 张宽大阶段卡 `全部活动 1 / Hub 待投递 0 / Agent 等资源 0 / 运行中 1` + 队列项 + 24h 诊断折叠栏，共 `~310px`）+ 双层筛选与同步控制栏（`~110px`）吞噬了近 **480px（约 50% 有效视口）**，导致核心审计列表首屏仅能展示 **4.5 张卡片**。空闲期（0 活动任务）时，4 个 `0` 统计块加 `.queue-empty` 留白仍占据 `~230px` 黄金首屏。

4. **双头刷新与双份连接状态提示**
   - **现象**：相距仅 `120px` 同时存在右上角「刷新队列」与列表上方「↻ 刷新」两个按钮，同时展示 `队列暂不可用：请求失败 (HTTP 502)` 与 `连接暂不可用，保留现有记录；稍后重试。` 两条语义重叠的报错文案；而「暂停实时更新」按钮放在筛选栏下方，却暗中同时控制顶部的 `CodePierQueue.setLive()`（`web/call-log.js:539`），视觉层级与控制半径倒置。

5. **徽章语义冲突与零信息熵视觉噪音**
   - **现象**：每条已完成记录左侧常驻 `[请求发起]` 灰签，右侧显示 `[已完成]` 绿签，用户扫视时极易将“请求发起（触发来源）”误读为“刚发起尚未完成（执行阶段）”；每行重复渲染 `项目 ` 二字前缀、同日重复渲染 `2026/11/18`、重复渲染与工具名同义的分类签（`exec` + `命令`、`write` + `写入`），无参工具（`open_workspace`）占用整行深色代码区显示 `没有可显示的参数摘要`。

6. **原始 `key=value` 参数摘要堆砌，淹没核心目标**
   - **现象**：`hub/call_log.py:42-73` 将业务主体（命令或文件路径）与默认样板参数拼成单行裸字符串 `command=git status ... cwd=. timeout_seconds=3600 commandChars=78`，默认项 `cwd=.` 与 `timeout_seconds=3600` 在每条命令中反复抢占截断预算。

---

### 0.2 从第一性原理出发的四大核心公理与约束方程

回归操作审计页的本质——它是开发者与管理员观测多智能体控制面行为的**实时飞行记录仪（Flight Recorder）与取证工作台**。其极限体验受以下四条第一性原理支配：

1. **关键渲染路径方程（Perceived Latency Minimization）**
   $$T_{\text{first-meaningful-paint}} = T_{\text{app-shell}}(0\text{ ms}) + \max\big(T_{\text{call-log}}, T_{\text{queue}}\big) \quad \text{而非} \quad T_{\text{skeleton-wipe}} + T_{\text{call-log}} + T_{\text{queue}}$$
   页面固定壳层（标题、筛选器、上次内存态快照）必须在路由切入的 **0 ms（同步帧）** 立即呈现；首次数据拉取必须单请求聚合或并行发起、单帧提交，严禁串行瀑布与二次撑开回流（CLS = 0）。

2. **局部增量调和公理（$O(|\Delta|)$ Zero-Wipe DOM Reconciliation）**
   筛选、搜索、翻页、实时流到达时，DOM 修改范围必须严格收敛在变化的数据集本身：
   $$\text{DOM Mutations} \propto |\Delta \text{Rows}|, \quad \Delta(\text{QueuePanel}, \text{FilterBar}, \text{Focus}, \text{IME}) = 0$$
   严禁因修改列表筛选条件而调用 `#page.innerHTML = html` 销毁重建整页及无关的队列组件。

3. **香农信息熵展示公理（Exception-Only Visual Signaling）**
   字段标签的视觉权重应正比于其信息熵 $H(X) = -\sum p(x)\log_2 p(x)$：
   - 当某属性取值占比 $>95\%$（如 `trigger_source = request`、`cwd = .`、`timeout_seconds = 3600`、当天的年月日、`active_queue = 0`），其单行重复展示的信息量趋近于 **0 bit**，应在列表态隐去，仅在偏离默认值（`自动维护`、子目录 `cwd`、非零退出码、超长耗时、跨天边界）时高亮呈现。

4. **单一事实源与边沿驱动公理（Unified Client Store & Epoch-Gated Stream）**
   同一工作空间内的 `SSE /api/events`、`活动队列` 与 `调用流水` 观测的是同一个 `operations` 状态机。前端必须以统一的 `OperationStore` 汇流所有实时事件与轮询快照；后端必须将只读鉴权与查询复杂度从“逐帧/逐行 $O(N)$ 重算”降维至“纪元门控 $O(1)$ + 批量集合查询”。

---

## 1. 全景优化项优先级总表（`AUD-01` 至 `AUD-24`）

| 编号 | 所属维度 | 优先级 | 核心源码定位 | 现状病灶与源码铁证 | 目标收益与量化验收指标 |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **AUD-01** | 加载与流式同步 | **P0** | `web/call-log.js:211-234`<br>`web/queue.js:423`<br>`web/app.js:1032-1035` | 切页先全屏刷 `uiLoadingPage` 白屏骨架；`call-log` 返回落 DOM 后才串行触发 `queue` 请求，二次撑开造成 `~280px` 布局跳动 | **0ms 壳层直出 + 单往返/并行首屏拉取**；首屏可交互耗时缩短 **~40–50%**，首屏 CLS 降至 **0** |
| **AUD-02** | 加载与流式同步 | **P0** | `web/call-log.js:401-410, 556`<br>`web/app.js:1107-1114` | `reset()` 与翻页调 `renderPage(false)`，而 `app.js:1110` 显式排除 `call-log` 走 DOM 调和，直接刷 `#page.innerHTML`，连带炸毁顶部队列与输入框焦点 | 局部 Keyed 调和 `.call-log`；筛选/翻页期间**队列零重刷、输入焦点与 IME 100% 保持**，120ms 延迟渐进进度条 |
| **AUD-03** | 加载与流式同步 | **P0** | `web/app.js:853, 874-886`<br>`web/call-log.js:334-398`<br>`web/queue.js:257-293` | SSE `operation` 载荷被丢弃；纯尾部防抖 `500ms` 在高频 `output` 下被无限重置饥饿；队列与列表状态孤立、重试策略不对称导致上下状态分裂 | 建立共享内存 `OperationStore`；SSE 状态跃迁 **0ms 原地生效**；`leading+trailing (300ms/1.2s)` 节流；折叠态忽略 `output` 广播减少 **90%+** 无效重查 |
| **AUD-04** | 加载与流式同步 | **P0** | `web/call-log.js:88-90, 146`<br>`web/queue.js:280-287` | 运行中耗时每 5s 跳变一次，502 断连时永久定格造成“仍在运行”假象；队列报错时旧卡片无陈旧态视觉区分 | 基于 `observed_at + performance.now()` 实现 **1s 丝滑单调跳秒**；断连自动切为“截至 HH:MM:SS 快照 · 重连倒计时”降级态 |
| **AUD-05** | 加载与流式同步 | **P1** | `web/call-log.js:374-394` | `window.scrollY < 60` 过严（滚过队列即失效），导致顶部队列有新任务而下方列表不显示 | 基于列表视口可见性与 `Scroll Anchoring` 像素级锚定插行；阅读态吸顶胶囊精确提示 `↑ 新增 K 条（含 M 条运行中）` |
| **AUD-06** | 加载与流式同步 | **P1** | `web/call-log.js:265-328`<br>`hub/call_log.py:381-453` | 点击展开有明显网络等待；运行中展开项每 5s 全量重拉 32KB 输出 + 200 条链路并整块重绘 | 悬停/聚焦 `120ms` 意图预取实现**点击 0ms 瞬开**；支持 `after_output_seq` / `after_event_id` 增量追加与贴底跟随 |
| **AUD-07** | 加载与流式同步 | **P1** | `web/app.js:2710-2758, 3321` | 切换「工具执行 / 全部事件」调 `clear()` 清空全部缓存与筛选，且「全部事件」退回旧版 `OFFSET` 表格与弹窗 | 双模式共享筛选与内存缓存实现 **<16ms 瞬时切换**；统一游标分页与内联详情抽屉 |
| **AUD-08** | 加载与流式同步 | **P2** | `web/call-log.js:235, 444` | 搜索需点按钮或回车，无一键清空，无键盘流导航 | `220ms` IME 安全即时搜索、一键清词与全局快捷键（`/`、`j/k`、`o/Enter`、`r`） |
| **AUD-09** | UI/UX 信息架构 | **P0** | `web/queue.js:52, 179-214`<br>`web/queue.css:28-76, 211` | 4 张大号阶段统计卡 + 空态盒子常驻占用 `230–420px`，严重挤压首屏审计流水 | 重构为**自适应折叠活动条（Adaptive Activity Strip）**：空闲态 `40px` 单行，活动态紧凑横条，首屏可见审计行提升至 **10–12 条（+140%）** |
| **AUD-10** | UI/UX 信息架构 | **P0** | `web/queue.js:53`<br>`web/call-log.js:232, 253` | 页面同时存在 2 个刷新按钮、2 套错误状态文字，暂停开关位置与控制范围错位 | 收敛为页头右侧**单一实时同步控制舱**（呼吸状态胶囊 + 暂停/恢复 + 统一刷新 + 导出菜单） |
| **AUD-11** | UI/UX 信息架构 | **P0** | `web/call-log.js:91-99, 179`<br>`web/call-log.css:62-97` | 每行常驻 `[请求发起]` 与右侧 `[已完成]` 语义冲突；重复 `项目` 前缀与同日日期；类型签与工具名冗余 | **零熵降噪**：隐藏默认 `请求发起`（仅显 `自动维护` 等非默认源）；色签直显项目名；同日仅显 `HH:mm:ss.SSS` 配合吸顶日期分隔线 |
| **AUD-12** | UI/UX 信息架构 | **P0** | `hub/call_log.py:42-73`<br>`web/call-log.js:143-145` | 裸拼 `key=value` 混入 `cwd=.` / `timeout_seconds=3600`；无参工具整行显示 `没有可显示的参数摘要` | **结构化语义摘要**：分工具族高亮路径/命令主体，剥离默认参数，字节数/改动数化作右侧轻量微签；无参调用自动收起次行（行高降至 `~56px`） |
| **AUD-13** | UI/UX 信息架构 | **P1** | `web/call-log.css:6-27, 99`<br>`web/call-log.js:146, 189` | 失败调用、60s 慢调用与普通 10ms 读取视觉权重完全一致，折叠态看不到错误原因 | **异常优先编码**：失败行语义左色条 + 折叠态单行错误/退出码直出；耗时分级着色 + 详情三段式横向耗时瀑布条（等待/执行/落盘） |
| **AUD-14** | UI/UX 信息架构 | **P1** | `web/call-log.js:189-209`<br>`web/app.js:2772` | 详情展开先被 11 项 `<dl>` 元数据占满首屏；且 `call-log.js` **漏接 `diffHTML`**（旧弹窗有，新内联详情无） | **详情信息流倒置**：错误告警 → 耗时瀑布与紧凑元数据条 → **补齐结构化 Diff 视图** / 带行号终端输出 → 链路时间轴 |
| **AUD-15** | UI/UX 信息架构 | **P1** | `web/call-log.js:235-252`<br>`web/queue.js:167` | 无快捷筛选预设，无法点击行内项目/工具快速过滤，筛选态不进 URL Hash；队列点“查看操作”弹出割裂旧弹窗 | 支持行内元素一键过滤、高频预设胶囊（`仅看异常`/`进行中`/`慢调用`）、`#audit?...` 脱敏深链，队列“查看操作”平滑定位展开对应行 |
| **AUD-16** | UI/UX 信息架构 | **P2** | `web/call-log.css` | 仅有卡片流单一视图，大屏横向对齐比对耗时/退出码效率受限 | 提供 **舒适卡片 / 高密表格（Dense Table）** 一键切换视图 |
| **AUD-17** | 后端查询与存储 | **P0** | `hub/call_log.py:106-141, 326-329` | 每次轮询必跑全量 `SELECT count(*)` 而前端完全未消费 `total`；`public_row()` 对 `args_summary` 执行**双重正则脱敏**后又将其 `pop` 丢弃 | 后台轮询跳过 `count(*)`，首屏改 `LIMIT 1001` 有界计数；消除 `public_row` 重复 `display_value` 与 `json.dumps`，单次查询 CPU 降低 **60%+** |
| **AUD-18** | 后端查询与存储 | **P0** | `hub/queue_panel.py:35-48`<br>`hub/runtime.py:617-695`<br>`hub/api/activity.py:121` | `/api/operations/queue` 循环内逐行重查 `operations` + 逐行查 `operation_events`（`3N+1` 次 SQL）；`/api/operations/{id}` **漏标 `read_only=True` 误抢全局写锁** | 队列接口改为已取字段内存鉴权 + 批量 `IN (...)` 聚合阶段，50 活动项从 `~130` 次 SQL 降至 **3 次**；补齐 `read_only=True` 释放写锁 |
| **AUD-19** | 后端查询与存储 | **P0** | `hub/api/activity.py:216-285`<br>`hub/runtime.py:271-280` | `/api/events` 每发一帧 SSE 均向 3 线程只读池提交完整 IAM SQL 事务；高频输出塞满 `Queue(100)` 触发全站 `event: resync` 惊群风暴 | 引入 `security_epoch` 内存权限快照实现常规帧 **$O(1)$ 零 SQL 放行**；Watcher 队列对同 ID `output/trace` 原地合并避免溢出 `resync` |
| **AUD-20** | 后端查询与存储 | **P1** | `hub/call_log.py:295-301, 372`<br>`hub/iam_schema.py:130, 176` | `SELECT DISTINCT o.tool` 无法利用时间前导索引导致全扫；`trigger_source` 靠运行时 `json_extract` 全扫；`audit` 表缺 `(space_id, id DESC)` 复合索引 | 维护空间级工具名轻量索引/缓存；物化 `trigger_source` 或表达式索引；为 `audit` 补齐 `(space_id, id DESC)` 与 `(space_id, owner_user_id, id DESC)` |
| **AUD-21** | 后端查询与存储 | **P1** | `web/call-log.js:531-536`<br>`hub/call_log.py` | 「导出本页调用」仅能导出 DOM 当前 40 条，跨页排障无法导出完整筛选集 | 新增 `/api/call-log-export` 只读流式脱敏导出（支持 JSONL / CSV，有界 10,000 条游标快照） |
| **AUD-22** | 第一性原理升维 | **P1** | `web/call-log.js`<br>`hub/call_log.py` | AI 智能体单轮任务连发 15–30 次 `read/search/exec` 铺满整屏，跨项目关键写入被刷没 | 新增**「按智能体会话突发（Burst / Turn）聚合」**开关：自动折叠同 `(actor, project)` 连续工具链，汇总读写数、总耗时与异常点 |
| **AUD-23** | 第一性原理升维 | **P2** | `web/call-log.js`<br>`web/queue.js:56-85` | 任务处于 `waiting_resource` / `waiting_project` 时，列表行仅显示泛化的“排队中”，需点开详情才知谁在阻塞 | 列表行内联渲染**可点击阻塞因果胶囊**（`等待 #a1b2c3 · exec`），一键跳转并高亮根阻塞操作 |
| **AUD-24** | 第一性原理升维 | **P2** | `web/queue.js:295-336` | 24h 实例队列诊断直接输出原始 `<pre>` JSON 字典，可读性差 | 改造为紧凑可视化诊断条（Worker Queue / Phase / Lock P50·P99 胶囊 + 等待原因占比条 + 根阻塞者直跳） |

---

## 2. 加载、刷新与实时同步链路深度优化（更快、更丝滑、零闪烁）

### 2.1 `AUD-01` [P0] 首屏 0ms 壳层直出 + 并行/聚合快照拉取（消除双 RTT 瀑布与 280px CLS）

- **现状剖析**：
  1. 用户点击侧栏「操作审计」时，`web/app.js:1032-1035` 先把 `#page.innerHTML` 替换为整页通用加载块 `uiLoadingPage('audit')`（销毁既有结构，造成整屏闪烁）。
  2. `CodePierCallLog.html(seq)`（`web/call-log.js:209`）阻塞等待 `/api/call-log` 返回后才拼出页面字符串，其中队列区仅放入静态占位 `<p data-queue-status>正在读取活动操作…</p>`（`web/queue.js:53`）。
  3. 待 `#page.innerHTML` 挂载完毕后，`CodePierCallLog.bind()` 才调用 `CodePierQueue.bind() -> refresh()` 发起第二个串行请求 `GET /api/operations/queue`（`web/queue.js:261`）。返回后 `.queue-panel` 高度从 `~72px` 暴涨至 `~280–420px`，把用户正准备点击的筛选栏和首条审计记录瞬间向下顶走。
- **优化设计**：
  1. **App-Shell 同步挂载（0ms 响应）**：切入 `#audit` 时，若 `S.callLog` 已有同会话内存缓存，**0ms 直接渲染缓存快照**并在后台静默 `revalidate`（Stale-While-Revalidate）；即使冷启动无缓存，也立即同步渲染真实页头、合并后的活动条骨架（固定 `40px` 最小高度）与完整筛选栏，仅在 `.call-log` 容器内渲染 6 条等高（`56px`）低对比度脉冲行骨架。
  2. **首屏请求合并（单 RTT 原子快照）**：在 `GET /api/call-log` 新增可选参数 `include_queue: bool = False`。首屏或全局手动刷新时传 `include_queue=true`，在同一次只读数据库 worker 调度中一并返回 `operations`、`projects`、`tools` 与 `queue` 快照；前端在同一渲染帧同时填充活动条与调用流水，首屏网络往返从 `2 × RTT` 降至 `1 × RTT`，布局偏移（CLS）严格为 **0**。

### 2.2 `AUD-02` [P0] 局部列表 Keyed 调和，彻底废除筛选/翻页时的 `#page.innerHTML` 全页重绘

- **现状剖析**：
  - `web/call-log.js:401-410` 的 `reset()`（由全部 5 个 `<select>` 的 `onchange`、搜索表单提交、「显示最新」按钮触发）与 `556-559` 的上下翻页均调用 `renderPage(false)`。
  - `web/app.js:1107-1114` 写了如下排他分支：
    ```js
    else if (
      !showLoading &&
      ['overview', 'devices', 'projects', 'audit', 'workflows'].includes(page) &&
      !(page === 'audit' && S.auditMode === 'operations' && window.CodePierCallLog)
    )
      CP.dom.replacePage($('#page'), html);
    else $('#page').innerHTML = html;
    ```
  - 结果：在「工具执行」页每次切换项目/工具/状态下拉框或翻页，都会执行 `$('#page').innerHTML = html`——**不仅销毁筛选栏本身（打断键盘连续 Tab 切换与搜索框中文输入法合成），还把顶部的 `#operation-queue` 连根拔起重置回“正在读取活动操作…”**，制造毫无必要的 `/api/operations/queue` 重拉与整页跳动！
- **优化设计**：
  1. **组件内聚局部刷新 `reloadList({ resetCursor })`**：筛选与翻页不再调用全局 `renderPage(false)`，而是在 `CodePierCallLog` 内部维护专属 `listAbortController`（连续切换筛选条件时立即 `abort()` 上一次未完成请求，杜绝慢请求乱序覆盖）。
  2. **渐进式非阻塞加载态（120ms 阈值）**：发起筛选请求后，若 `120ms` 内返回则直接丝滑替换（零加载动画闪烁）；若超过 `120ms`（如慢网或复杂搜索），仅在 `.call-log` 顶部激活一条 `2px` 细流光条并将旧列表透明度平滑过渡至 `0.68`（保持原高度不坍塌）。
  3. **Keyed DOM 节点复用**：为每条 `<details class="call-entry">` 赋予稳定 `data-cp-key="call:${r.id}"`，仅对 `.call-log` 子树调用 `CP.dom.replacePage(listContainer, rowsHTML)`（或轻量 Keyed patch），已存在于本页的条目保留 DOM 实例与展开详情缓存，顶部队列与筛选控件焦点纹丝不动。

### 2.3 `AUD-03` [P0] 前端统一 `OperationStore` + SSE 载荷直驱 + `leading+trailing` 节流状态机

- **现状剖析**：
  - `web/app.js:874-886` 对 `['operation', 'output', 'trace', 'device', 'project']` 统一采用无上限延后的纯 debounce（`clearTimeout(S.eventTimer); setTimeout(..., 500)`）。当长命令每隔 `150–300ms` 吐出一块 stdout `output` 时，`S.eventTimer` 被持续清零重设，导致实时更新在命令最活跃阶段反而**被饿死长达数秒**；而在稀疏输出场景下，折叠列表根本不展示 `output`，却因每次 `output` 事件反复全量请求 `/api/call-log`！
- **优化设计**：
  1. **建立单页共享 `OperationStore`（仅驻留当前认证标签页内存，严守安全边界不落盘）**：
     - 统一管理 `byId: Map<opId, OperationRecord>`、`activeIds: Set<opId>`、`lastSyncedAt: number` 与 `connectionHealth: 'live' | 'reconnecting' | 'paused'`。
     - `CodePierQueue`（活动条）与 `CodePierCallLog`（调用流水）以及侧边栏状态点 `navActivity` 全部订阅同一 `OperationStore`。一旦队列快照发现某 `id` 已不在活动集合，或 SSE 推来终态，三处 UI **同一帧同步收敛**，彻底根绝截图中“队列已无该任务、下方仍显示执行中”的分裂现象。
  2. **SSE 事件分级精确路由**：
     - **`m.type === 'operation'`**：后端 `_publish("operation", ...)` 附带轻量已脱敏状态字段 `{ id, project_id, tool, state, created, updated, exit_code, queue_stage }`。前端收到后 **0ms 立即原地更新**已渲染行的状态徽章、退出码与队列计数；仅当出现未知新操作 ID 时，按 `leading + trailing throttle (interval: 300ms, maxWait: 1200ms)` 合并拉取一次列表头部增量。
     - **`m.type === 'output' | 'trace'`**：**仅当 `opened.has(m.data.id)`（用户正展开阅读该条详情）时**，按 `400ms` 节流触发该单条记录的增量详情刷新；若该记录处于折叠态，前端**直接忽略**该帧，实现零多余 HTTP 请求！
  3. **统一重试与退避协调器**：移除 `queue.js` 与 `call-log.js` 各自为政的轮询定时器，合并为单一自适应调度器：SSE `live` 正常时补偿轮询间隔放宽至 `15s`（降低空转负载 66%）；SSE 断连或遇 502 时切换至带抖动的指数退避（`1s → 2s → 4s → 8s`，上限 `10s`），并在恢复瞬间立即触发一次 `include_queue=true` 全量对齐。

### 2.4 `AUD-04` [P0] 本地单调时钟平滑跳秒与离线陈旧快照（Stale State）诚实反馈

- **设计要点**：
  - 每次收到服务端快照时记录基准映射：`clockAnchor = { serverNow: r.observed_at, monoNow: performance.now() }`。
  - 当页面可见且存在 `ACTIVE_STATES` 行时，启动单一轻量 `1000ms` 节拍器（`document.hidden` 时自动挂起），仅更新页面上带 `[data-live-created]` 属性的文本节点：
    $$\text{liveElapsedMs} = \max\big(0, \big(\text{serverNow} + \frac{\text{performance.now()} - \text{monoNow}}{1000} - \text{created}\big) \times 1000\big)$$
    配合 `font-variant-numeric: tabular-nums` 等宽数字，运行耗时如秒表般每秒丝滑递增，零重排、零网络开销。
  - **断连诚实降级**：一旦最近一次同步失败（如 `HTTP 502`）或 SSE 断开超过 `6s`，停止假性跳秒，在同步胶囊显示 `🟡 服务更新/重连中 · 保留 16:21:21 快照（3s 后重试）`，并将活动条与运行中徽章转为温和的虚线边框态（`截至断连前 10.4s`），既保留已有排障证据，又绝不误导用户。

### 2.5 `AUD-05` ~ `AUD-08` [P1/P2] 阅读锚定插行、悬停意图预取与即时交互体验

- **无感滚动锚定插行（`AUD-05`）**：
  - 废弃 `window.scrollY < 60` 硬编码。改为检测 `.call-log` 首条记录顶部是否在视口内（`firstRow.getBoundingClientRect().top >= 0`）且无展开项：若是，直接平滑前插新行；若用户已向下滚动或展开了某条记录，则在插入新行前后记录锚点元素 `anchorTop = anchorEl.getBoundingClientRect().top`，插入后同步修正 `window.scrollBy(0, anchorEl.getBoundingClientRect().top - anchorTop)`，或展示带精确计数的吸顶悬浮胶囊 `↑ 3 条新调用（Nexus · exec 等）· 点击查看`。
- **悬停/聚焦 120ms 意图预取 + 增量详情流（`AUD-06`）**：
  - 当指针移入 `<summary>` 停留满 `120ms`（过滤快速扫过）或键盘 `focus-visible` 停留满 `120ms` 时，若 `cache` 未命中且当前并发预取数 `< 2`，后台静默预取 `/api/call-log/{id}`。实测人类从悬停瞄准到点击平均耗时 `180–320ms`，点击瞬间详情已在内存中，实现 **0ms 瞬时展开**。
  - `/api/call-log/{id}` 增加可选查询参数 `after_output_seq` 与 `after_event_id`：对展开中的运行态命令，每次仅传输新增输出分片（通常 `< 2KB`）并调用 `pre.append(textNode)`，避免每 5 秒全量重传 32KB 文本与重刷 DOM；当用户滚动条处于底部阈值（`scrollHeight - scrollTop - clientHeight < 24`）时自动贴底跟随，向上翻阅历史输出时自动解除跟随并浮现 `↓ 跳至最新输出` 小按钮。
- **双模式瞬切与即时搜索（`AUD-07`、`AUD-08`）**：
  - 切换「工具执行 / 全部事件」不再调用 `CodePierCallLog.clear()` 销毁缓存（仅在 `S.session` 或 `S.space_id` 变更时清空），保留两边各自独立游标与筛选缓存，切换耗时 `< 16ms`。
  - 搜索框监听 `input`（守卫 `!e.isComposing`，在 `compositionend` 后触发），采用 `220ms` 防抖自动检索，内置一键清空 `×` 按钮；绑定快捷键 `/`（聚焦搜索）、`j / k`（上下移动调用行焦点）、`Enter / o`（展开/收起当前焦点行）、`r`（立即同步）。

---

## 3. UI/UX 信息架构与视觉交互深度重塑

### 3.1 页面空间重构前后对比（1080p 视口）

```text
【重构前：首屏 ~480px 被头重脚轻的面板占据，仅见 4.5 条日志，两套刷新与两套报错并存】
┌──────────────────────────────────────────────────────────────────────────┐
│ 操作审计                                                  [↓ 导出本页调用] │ ~68px
├──────────────────────────────────────────────────────────────────────────┤
│ 当前队列                                                       [刷新队列] │
│ 队列暂不可用：请求失败 (HTTP 502)                                          │ ~310px
│ [全部活动 1]   [Hub 待投递 0]   [Agent 等资源 0]   [运行中 1]              │
│ ┌ Nexus [exec] Studio                                     • 执行中       ┐│
│ │ command=git worktree list ... cwd=. timeout=3600       #0940ffd50dea  ││
│ └                                                      [查看操作][请求取消]┘│
│ ▶ 实例队列诊断（24 小时）                                                  │
├──────────────────────────────────────────────────────────────────────────┤
│ [工具执行|全部事件] [全部项目▾] [全部工具▾] [全部状态▾] [搜索框...] [搜索][更多▾]│ ~110px
│ [暂停实时更新] [↻ 刷新] 连接暂不可用，保留现有记录；稍后重试。                  │
├──────────────────────────────────────────────────────────────────────────┤
│ (仅剩约 420px 视口高度，每张卡片 ~98px，首屏仅能看到 4.5 条调用卡片)            │
└──────────────────────────────────────────────────────────────────────────┘

【重构后：统一同步舱 + 自适应紧凑活动条 + 高信噪比行布局，首屏直出 10–12 条日志】
┌──────────────────────────────────────────────────────────────────────────┐
│ 操作审计                    [🟢 实时同步 16:25:24 · ⏸] [↻] [↓ 导出 ▾]     │ ~52px
├──────────────────────────────────────────────────────────────────────────┤
│ ⚡ 活动队列 (1)  [全部 1] [运行中 1] · 待投递 0 · 等资源 0      [24h 诊断 ▾]│ ~44px~84px
│   └─ [Nexus] exec · Studio  `git worktree list --porcelain …`  ⏱ 12.4s [定位][取消]│ (空闲时仅 38px)
├──────────────────────────────────────────────────────────────────────────┤
│ [工具执行|全部事件] [项目▾] [工具▾] [状态▾] [🔍 搜索路径/命令/编号 ×] [更多▾]│ ~52px
│ 快捷视图：[全部] [🔴 异常/失败] [🟡 进行中] [⏱ 慢调用 >5s] [✍ 文件写入]     │ ~34px
├──────────────────────────────────────────────────────────────────────────┤
│ ── 2026-11-18 今天 ───────────────────────────────────────────────────── │
│ [SVG]   write  `artworks/pelican-coastal-post.svg`   +11.6KB  ✓ 已完成  527ms ⌄│ ~54px/行
│ [Nexus] exec   `pwd && rg --files --hidden …`                 • 执行中  10.4s ⌄│ (首屏容纳
│ [Lumen] exec   `printf '1.2.193\n' > VERSION && …`   退出码 1 ✕ 失败    64.3s ⌄│  10~12 条!)
│ [Nexus] open_workspace  Studio · ChatGPT · #82fe646b          ✓ 已完成  4.01s ⌄│
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 `AUD-09` & `AUD-10` [P0] 自适应活动条（Adaptive Activity Strip）与顶栏统一同步控制舱

1. **自适应活动条（取代常驻巨型 `.queue-panel`）**：
   - **空闲态（`active === 0`）**：折叠为单行 `38px` 轻量状态条：`✓ 当前队列空闲 · 无排队或执行中操作`，右侧内联 `24h 队列诊断 ▾` 入口，释放 **~200px** 垂直空间。
   - **活动态（`1 <= active <= 3`）**：头部同行渲染紧凑胶囊筛选（计数为 `0` 的阶段降为浅灰文字，有计数的阶段高亮胶囊），下方每条活动任务采用**单行紧凑横条**（项目色签 + 工具名 + 单行截断命令 + 实时跳秒计时 + `[定位详情]` `[取消]`），不再渲染多层嵌套灰框。
   - **繁忙/拥塞态（`active > 3` 或存在 `agent_waiting` 阻塞）**：自动展开高密度可滚动托盘（`max-height: 240px`），置顶展示被阻塞任务与阻塞源提示。
2. **顶栏统一实时同步控制舱**：
   - 删除队列面板右上角的独立「刷新队列」按钮与列表上方的 `.call-controls` 整行，合并至页头右侧：
     - **连接健康胶囊**：`🟢 实时` / `🟡 重连中（保留 16:21 快照）` / `⏸ 已暂停`（悬停显示精确最后同步毫秒时间与 SSE 通道状态）。
     - **一键暂停/恢复图标按钮** + **全局刷新按钮**（并行刷新队列与当前筛选列表，带旋转微动效）。
     - **分体式导出菜单**：`导出当前页 JSON` / `导出当前筛选全集 CSV·JSONL`。

### 3.3 `AUD-11` & `AUD-12` [P0] 零熵徽章降噪与结构化语义参数摘要

1. **消除徽章歧义与冗余（`AUD-11`）**：
   - **触发来源“异常才显”**：当 `trigger_source === 'request'` 时不在折叠行渲染 `[请求发起]` 灰签（消除与右侧 `[已完成]` 的语义打架，完整字段仍保留在展开后的元数据栏）；当 `trigger_source === 'system_maintenance'` 时醒目展示 `[⚙ 自动维护]`，无模型调用展示 `[本地程序]`。
   - **项目色签去前缀化**：`<span class="call-project"><strong>Nexus</strong></span>` 移除每行重复的 `项目` 小字（保留 `aria-label="项目 Nexus"` 屏幕阅读器语义，并严格维持现有 12 色调色板与亮暗色 `WCAG AA >= 4.5:1` 对比度契约）。
   - **工具类型融合**：通过工具名左侧微图标或色点（读取=蓝、写入=绿、编辑=琥珀、命令=紫、地图=青）传达类别，省去与 `exec/write/read` 紧邻重复的中文灰签，仅对复合或不直观工具名保留辅助说明。
   - **智能时间轴**：列表按本地自然日自动插入吸顶轻量分隔条（`今天 · 2026-11-18`、`昨天 · 2026-11-17`），行内时间仅展示等宽 `16:21:21.429`（悬停 `title` 显示完整 ISO/本地日期时间），每行节省 11 个字符宽度。
   - **操作编号一键复制芯片**：点击 `#fb23f6b1` 直接复制完整 32 位 `operation_id` 并原位浮现 `✓ 已复制` 微提示（`event.stopPropagation()` 不误触折叠展开）。

2. **结构化语义参数摘要渲染器（`AUD-12`）**：
   - 在 `hub/call_log.py` 输出结构化轻量摘要字段 `summary_parts: { primary, kind, badges: [] }`（同时保留平铺字符串 `summary` 兼容旧导出与测试）：
     - **命令类（`exec` / `shell_exec` / `ssh_exec` / `tasks_run`）**：主区高亮渲染脱敏后的命令串 `git worktree list --porcelain && git status …`；自动隐藏默认值 `cwd=.` 与默认 `timeout_seconds=3600`，仅当 `cwd !== '.'` 时显示 `[cwd: web/mcp-apps]`，右侧辅以微签 `112 字符`。
     - **文件读写编辑类（`read` / `write` / `edit` / `apply_patch`）**：主区突出文件路径（目录前缀弱化色、文件名加粗，如 `artworks/`**`pelican-coastal-post.svg`**），右侧展示微签 `L1–120`、`3 处编辑` 或 `11.4 KB`（将 `inputChars=11634` 转为人类易读量级）。
     - **搜索类（`search` / `fs_search`）**：主区展示查询词 `🔍 "操作审计"` + 路径范围微签 `[path: web]`。
     - **无参工具（`open_workspace` / `project` / `whoami` / `targets`）**：不渲染空代码行，将次行元数据（时间、设备、授权身份、编号）优雅内联至紧凑单行或双行布局，彻底消灭 `没有可显示的参数摘要` 占位废话。

### 3.4 `AUD-13` ~ `AUD-16` [P1/P2] 异常优先编码、详情流倒置补齐 Diff、快捷过滤与双密度视图

1. **异常优先视觉编码与三段式耗时微瀑布（`AUD-13`）**：
   - `state === 'failed' | 'interrupted'` 卡片左侧赋予 `3px solid var(--ui-danger)` 语义色条，并在折叠行直接单行展示 `退出码 1 · <error 首行摘要>`，无需逐一点开排查报错。
   - 耗时数字语义分级：`< 1s` 次要灰、`1s–10s` 常规色、`>= 10s` 琥珀色加粗（慢调用一眼锁定）。
   - 展开详情顶部绘制 **三段式横向耗时比例条**：
     - `[Hub 排队与投递 wait_ms]`（浅蓝） + `[Agent 实际执行 execution_ms]`（主色） + `[回传与落盘调度余量]`（浅灰），直观区分“电脑执行慢”与“队列锁等待慢”。

2. **详情面板信息流倒置与补齐结构化 `diffHTML`（`AUD-14`）**：
   - **修复功能断层**：目前 `web/app.js:2772` 的旧版弹窗详情会渲染 `r.result?.data?.diff !== undefined ? diffHTML(r.result.data) : ''`，但新版内联详情 `web/call-log.js:189-209` **遗漏了 Diff 渲染**，导致在审计页展开 `edit` / `apply_patch` 记录只能看生硬的 JSON 字符串。立即在 `call-log.js` 详情首屏接入脱敏后的红绿增删 `diffHTML` 视图。
   - **调整详情阅读动线**：将原有的 11 项双列大号 `<dl class="call-facts">` 压缩为单行紧凑元数据胶囊带（`触发来源` · `授权身份` · `完整编号 [复制]` · `投递次数` · `当前阶段`），把首屏黄金位置让给 **① 错误详情 → ② 文件差异（Diff）/ 终端输出（带行号与页内搜索） → ③ 参数摘要 → ④ 执行链路时间轴**。

3. **行内就地过滤、快捷预设胶囊与 URL Hash 深链（`AUD-15`）**：
   - 筛选栏下方增设一键预设胶囊：`全部`、`🔴 异常与失败`（聚合 `failed` + `interrupted` + `needs_review`）、`🟡 进行中`（聚合全部 `ACTIVE_STATES`——弥补当前下拉框只能单选某一个子状态的痛点！）、`⏱ 慢调用 (>5s)`、`✍ 写入与命令`。
   - 将非敏感筛选参数同步至路由 Hash（如 `#audit?project=p1&preset=active&op=0940ffd50dea`）：刷新页面不丢筛选态；点击顶部活动条的「定位详情」直接平滑滚动并展开下方对应记录（若不在当前筛选集则内联抽屉打开 `/api/call-log/{id}`），废除割裂的旧版 `/api/operations/{id}` 模态框。

4. **双密度模式切换（`AUD-16`）**：
   - 页头工具栏提供 `[卡片视图 | 紧凑表格]` 密度切换：紧凑表格模式将 `时间 | 项目 | 工具 | 摘要 | 授权 | 状态 | 耗时` 严格按列对齐，行高压缩至 `38px`，单屏可同时检视 **18–22 条**调用记录，展开行直接在表格下方内嵌滑出详情区。

---

## 4. 后端查询、存储索引与 SSE 推送热路径第一性原理优化

### 4.1 `AUD-17` [P0] 消除 `/api/call-log` 无效全量 `count(*)` 与 `public_row` 双重正则脱敏

- **源码铁证 1（无效全表/全索引计数）**：
  - `hub/call_log.py:326-329`：
    ```python
    count_sql = "SELECT count(*) AS n FROM operations o" + joins + " WHERE " + " AND ".join(where)
    total = store.all(count_sql, values)[0]["n"]
    ```
  - 即使前端每 5 秒后台轮询时显式传了 `include_filters=false`，该端点依然每次扫描全空间历史操作计算精确 `total`，而 `web/call-log.js` 全文**没有任何一处读取 `r.total`**！
  - **修复方案**：新增参数 `include_total: bool = False`（或仅当 `include_filters=True` 且无 `cursor` 时计算），且将无界 `count(*)` 改为有界子查询 `SELECT count(*) AS n FROM (SELECT 1 FROM operations o ... LIMIT 1001)`，前端展示精确数（`<= 1000`）或 `1,000+ 项`，将大库计数耗时从 $O(N)$ 封顶为常数 `< 1ms`。

- **源码铁证 2（`public_row` 双重正则脱敏与无效序列化）**：
  - `hub/call_log.py:106-141`：
    1. 第 106 行：`args, clipped, scrubbed = display_value(_object(result.get("args_summary")), text_limit=1200, budget=12000)` 已对 `args_summary` 完整跑完 7 组敏感词与凭据正则；
    2. 第 109–110 行：把 `args` 塞回 `result["args_summary"]`，并用已脱敏的 `args` 生成 `result["summary"]`；
    3. 第 122 行：`json.dumps(args, ensure_ascii=False)` 仅为了检测 `"…" in ...`；
    4. 第 135 行：`result, more_clipped, more_scrubbed = display_value(result, text_limit=1600, budget=16000)` **对整包 `result`（包含刚脱敏过的 `args_summary`、`summary` 以及纯十六进制 `id`/`project_id`）再跑第二遍全套正则树遍历**；
    5. 第 140 行：`result.pop("args_summary", None)` 把刚刚二次脱敏完的 `args_summary` 直接丢弃！
  - **修复方案**：
    - 列表态 `public_row(..., include_args=False)` 先提取 `_log` 元数据并生成 `argument_summary`，**绝不将 `args_summary` 塞入待二次扫描字典**；
    - 仅对可能含外部自由文本的字段（`error`、`transport_error`、`actor`、`alias`、`device_name`）调用单次 `redact_text`，UUID/数值/枚举状态字段直接透传；
    - 在 `safe_summary` 截断阶段或递归遍历时顺带标记 `has_ellipsis` 布尔位，废除第 122 行额外的 `json.dumps(args)`。单次 80 行（40 列表 + 40 `watch`）序列化 CPU 开销预计下降 **60%–70%**。

### 4.2 `AUD-18` [P0] 根治 `/api/operations/queue` 的 `3N+1` 查询风暴与 `/api/operations/{id}` 误抢写锁

- **源码铁证 1（队列接口 `3N+1` 次 SQLite 查询）**：
  - `hub/queue_panel.py:35-48` 在遍历最多 `1,000` 条活动操作时：
    1. 逐行调用 `runtime.operation_row(row["id"], principal, status_only=True)` —— 明明第 24 行刚从 `operations` 表查出该行，这里又按主键发 `N` 次 `SELECT * FROM operations WHERE id=?`！
    2. 逐行调用 `runtime.queue_annotations(row, principal)`（`hub/runtime.py:620`）—— 逐行发 `N` 次 `SELECT stage,at,detail FROM operation_events WHERE operation_id=? AND source='agent' ORDER BY seq DESC LIMIT 1`，若处于等待态再对每个阻塞列表发子查询！
  - **修复方案**：
    1. 抽出纯内存策略校验函数 `runtime.authorize_loaded_operation_row(row, principal)`（第 24 行 SQL 补齐 `grant_id, space_id, owner_user_id, visibility` 4 列即可完全在内存完成与 `operation_row` 等价的细粒度角色与 `computer` 权限判定，**零额外 SQL**）。
    2. 对通过鉴权的最多 `limit`（100）个活动 ID，使用**单条批量 SQL** 一次性拉取各自最新 Agent 阶段事件：
       ```sql
       SELECT e.operation_id, e.stage, e.at, e.detail
       FROM operation_events e
       JOIN (
         SELECT operation_id, MAX(seq) AS max_seq
         FROM operation_events
         WHERE source = 'agent' AND operation_id IN (?, ?, ...)
         GROUP BY operation_id
       ) latest ON e.operation_id = latest.operation_id AND e.seq = latest.max_seq
       WHERE e.source = 'agent'
       ```
    3. 收集所有等待项引用的去重 `blocked_by` ID 集合，合并为**单次** `_visible_operations` 批量查询。50 个并发活动操作下，单次队列轮询 SQL 次数从 `~130 次` 降至固定 **3 次**！

- **源码铁证 2（读操作详情误抢全局唯一写锁）**：
  - `hub/api/activity.py:120-122`：
    ```python
    @router.get("/api/operations/{id}")
    @database_endpoint(store)  # <-- 漏写 read_only=True！默认走写线程 StoreWorker！
    def operation(id: str, request: Request):
        return runtime.operation(id, auth.panel(request))
    ```
  - 当后台有大文件导入或高频状态写入持有写锁时，用户在队列点击「查看操作」会被阻塞在单写队列之后，反过来也增加写线程排队延迟。
  - **修复方案**：立即改为 `@database_endpoint(store, read_only=True)`（经核对 `runtime.operation()` 标记为 `@iam.read_decision`，纯只读无写副作用），并将审计页队列详情交互统一收敛至 `/api/call-log/{id}`。

### 4.3 `AUD-19` [P0] `/api/events` SSE 纪元门控（Epoch-Gated）零 SQL 鉴权与队列原地合并

- **源码铁证**：
  - `hub/api/activity.py:249-280` 在 `while not runtime.stopping:` 循环中，**每收到 1 帧事件（含高频 `output`、`trace` 以及每 15 秒一次的空闲心跳）**，都调用 `await store.run_read(snapshot, item)` 进入 3 线程只读连接池重新查询会话、空间成员关系与全部项目权限表！
  - 当命令密集输出时，异步消费者因等待 `store.run_read` 变慢，瞬间填满 `asyncio.Queue(maxsize=100)`；`hub/runtime.py:272-279` 随即清空队列并压入 `{"type": "resync"}`，迫使浏览器端触发全站 5 接口重拉风暴（`web/app.js:808-824`）。
- **修复方案**：
  1. **基于 `iam_epoch` 的内存权限快照缓存**：在 `runtime` 维护单调递增计数器（每当会话注销、成员角色变更、Grant 撤销、项目删除或密码轮换提交时在 `after_commit` 自增 `iam_epoch`）。SSE 连接建立时运行一次 `store.run_read(snapshot)` 缓存 `(principal, access_set, cached_epoch)`；后续事件帧若 `cached_epoch == runtime.iam_epoch` 且会话未过期，直接在事件循环内用纯内存集合运算（耗时 `< 5 μs`）完成 `_audience` 与项目可见性判定，**零 SQLite 访问**！仅当 `cached_epoch != runtime.iam_epoch` 时才回查只读池校验是否降权并决定是否发送 `access_revoked`。
  2. **Watcher 队列按 `(type, operation_id)` 原地合并（Coalescing）**：对 `output` 与 `trace` 这类幂等通知帧，入队前若队列中已存在同一 `operation_id` 的未消费 `output/trace` 标记，直接合并更新时间戳/最新序号而不新增槽位；保留完整容量给关键状态帧（`operation`、`iam`、`computer_approval`），从根源上消除高频标准输出引发的伪 `resync` 风暴。

### 4.4 `AUD-20` & `AUD-21` [P1] 索引补齐、工具字典加速与全集流式导出

1. **消除 `SELECT DISTINCT o.tool` 全索引扫描**：
   - `hub/call_log.py:372-377` 因 `operations_panel_timeline` 前导列为 `(space_id, created DESC, id DESC, ...)`，查询 `DISTINCT o.tool` 必须遍历该空间全部历史索引项。
   - **修复**：新增极窄覆盖索引 `CREATE INDEX IF NOT EXISTS operations_space_tools ON operations(space_id, tool, project_id)`（SQLite 支持高效的松散索引跳跃或极小 B 树扫描），或在内存按 `space_id` 缓存已出现工具集合（新工具入库时增量 `add`），查询耗时降至 `< 0.2 ms`。
2. **补齐 `audit` 表（「全部事件」）多租户时间倒序索引与游标分页**：
   - 目前 `audit` 表仅有 `audit_recent(at DESC)` 与 `audit_space(space_id)`，执行 `WHERE space_id=? ORDER BY id DESC LIMIT 41` 缺少联合索引，且分页仍使用随页码线性变慢的 `OFFSET`。
   - **修复**：新增 `CREATE INDEX IF NOT EXISTS audit_space_timeline ON audit(space_id, id DESC)` 与 `CREATE INDEX IF NOT EXISTS audit_owner_timeline ON audit(space_id, owner_user_id, id DESC)`，并将 `/api/audit` 升级为 `before_id` 稳定游标分页（复用 `/api/audit-export` 已验证的 `before_id` 语义）。
3. **新增 `/api/call-log-export` 筛选全集流式导出（`AUD-21`）**：
   - 复用 `/api/call-log` 完全一致的 IAM 范围、筛选条件与 `public_row` 脱敏管线，在只读池按游标分批读取（上限 `10,000` 条，响应头附带 `X-Export-Truncated` 与 `X-Export-Next-Cursor`），支持导出 `NDJSON` 或 `CSV`，同时写入一条 `call_log.exported` 审计记录，补齐「工具执行」跨页取证闭环。

---

## 5. 从第一性原理出发的高阶可观测性演进

1. **`AUD-22` [P1] 智能体会话突发聚合视图（Agent Burst / Turn Grouping）**
   - **洞察**：在 MCP 辅助研发场景下，一次用户指令往往触发同一智能体授权（如 `mcp:grant_id:ChatGPT`）在同一项目（如 `Nexus`）内连续执行 `project → tree → search → file(×4) → edit → exec(pytest)` 共 10–30 次调用。平铺列表将“1 个高层意图”拆碎成满屏原子调用，掩盖了跨项目并行动态。
   - **方案**：在不改动底层不可变审计记录的前提下，前端提供默认开启或一键切换的**「聚合连续读取 / 按批次折叠」**模式：
     - 聚合判据：相邻调用满足同一 `(actor, project_id, device_id)`、间隔 `< 15s` 且无失败中断的连续只读探查链（或共享同一 `workflow_id`）。
     - 聚合卡片展示：`[Nexus] ChatGPT 连续探查 8 次（3 搜索 · 5 读取） · 累计耗时 1.42s`，点击可原地展开全部原始原子调用，失败或写操作始终独立置顶高亮。

2. **`AUD-23` [P2] 行内阻塞因果胶囊与一键追根（Inline Contention & Root-Blocker Jump）**
   - **洞察**：CodePier 拥有先进的自适应联合资源调度器（`waiting_resource`、`waiting_project`、`waiting_worker`），但在审计列表行上，排队任务仅笼统显示 `排队中`，用户不知道它为何不动。
   - **方案**：在 `/api/call-log` 对处于 `queued` 状态的可见行直接附带轻量 `waiting` 摘要（复用批量化后的 `queue_annotations`）。列表行状态区旁直接显示 `[⏳ 等待文件锁 · 被 #315212 (exec) 阻塞]` 胶囊，点击即可高亮滚动至阻塞源操作，将调度器内部状态透明转化为用户可感知、可干预（如取消根阻塞慢命令）的直观动作。

3. **`AUD-24` [P2] 24h 实例队列诊断可视化微仪表盘**
   - 将 `web/queue.js:295-336` 中生硬的 JSON `<pre>` 文本改造为三栏迷你指标组：
     - **吞吐与慢等待**：24h 总操作数、`>10s` 慢等待率、车队阻塞占比（Convoy Ratio）进度环；
     - **SQLite 存储健康**：Worker 排队、Phase 执行、写锁等待的 `P50 / P99` 毫秒对比胶囊（超阈值自动标琥珀色）；
     - **根阻塞者排行（Top Blockers）**：可点击 `#operation_id` 直接跳转至对应审计详情核查慢操作根因。

---

## 6. 分波次无损落地路线图（Three-Wave Rollout）

### Wave 1：极速止血、零闪烁丝滑加载与首屏降噪（纯前后端热路径重构，零破坏性 Schema 迁移）
- **后端先行**：
  1. 落地 **`AUD-17`**（移除后台轮询无效 `count(*)`，重构 `public_row` 单次精准脱敏）与 **`AUD-18`**（`/api/operations/queue` 批量化至 3 条 SQL，`/api/operations/{id}` 补齐 `read_only=True`）。
  2. `/api/call-log` 支持 `include_queue=true` 首屏单次合并返回（支撑 **`AUD-01`**）。
- **前端闭环**：
  3. 落地 **`AUD-01` + `AUD-02` + `AUD-03` + `AUD-04`**：0ms 壳层直出、局部列表 Keyed 调和（保队列、保焦点）、共享 `OperationStore` + SSE `operation` 直驱更新 + `1s` 本地单调跳秒与断连陈旧态诚实提示。
  4. 落地 **`AUD-09` + `AUD-10` + `AUD-11` + `AUD-12`**：自适应折叠活动条、顶栏统一实时同步舱、隐藏默认 `[请求发起]` 消除歧义、结构化语义摘要与同日紧凑时间戳。

### Wave 2：流式详情、SSE 纪元门控与索引/导出补齐
- 落地 **`AUD-19`**（`/api/events` 基于 `iam_epoch` 的 $O(1)$ 内存鉴权与 `output/trace` 队列原地合并，根除高负载 `resync` 风暴）。
- 落地 **`AUD-05` + `AUD-06` + `AUD-13` + `AUD-14` + `AUD-15`**：滚动锚定插行、悬停 `120ms` 意图预取、增量 `after_output_seq` 贴底输出流、补齐内联 `diffHTML`、三段式耗时瀑布条与快捷过滤预设/Hash 深链。
- 落地 **`AUD-20` + `AUD-21`**：补齐 `operations_space_tools` 与 `audit_space_timeline` 索引，上线 `/api/call-log-export` 筛选全集流式导出与双模式瞬切（**`AUD-07`**）。

### Wave 3：高阶可观测性升维与效率极客体验
- 落地 **`AUD-22`**（智能体连续调用批次折叠）、**`AUD-23`**（行内阻塞因果直跳）、**`AUD-24`**（24h 队列诊断图形化）以及 **`AUD-08` + `AUD-16`**（键盘流快捷键与紧凑表格双密度模式）。

---

## 7. 安全隐私红线与自动化验证门禁

所有优化实施必须严格遵守既有安全契约并通过以下自动化验证门禁：

1. **不可妥协的五条安全与一致性红线**
   - **零持久化客户端缓存**：`OperationStore`、预取详情缓存与双模式快照仅存于当前认证标签页闭包内存，**严禁写入 `localStorage` / `sessionStorage` / `IndexedDB`**；URL Hash 仅允许同步枚举筛选态与公开 `operation_id`，严禁写入敏感搜索词或参数片段；退出登录、`401`、`access_revoked` 或切换工作空间时必须同步清空全部内存态（保持 `test_late_detail_after_logout_cannot_repopulate_the_page` 100% 通过）。
   - **零持久载荷解密**：列表与详情摘要展示严禁解密冻结队列请求体，严禁因读取详情或轮询而重放工具或产生新工具调用审计。
   - **先脱敏后截断**：任何命令、输出、结果与错误摘要在切片截断前必须先经 `shared/audit_redaction.py` 脱敏，防止凭据被截断破坏正则边界后泄露前缀。
   - **真实回执优先**：客户端本地跳秒仅作用于 `ACTIVE_STATES` 的视觉插值；总耗时、等待耗时与实际执行耗时在终态一律以服务端持久化证据为准，缺失阶段显示“未记录”，绝不臆测补造。
   - **无障碍与主题对比度**：所有项目色签、状态徽章与紧凑表格在亮/暗双主题及 `320px / 390px / 1280px` 视口下无横向溢出（`scrollWidth <= innerWidth`），文本对比度满足 `WCAG AA >= 4.5:1`，完整支持 `prefers-reduced-motion: reduce` 与纯键盘操作。

2. **必跑验证命令集**
   ```sh
   # 1. 重新生成并校验前端静态资源哈希清单
   python scripts/build_web_assets.py
   python scripts/build_web_assets.py --check

   # 2. 运行调用日志、队列面板与全栈浏览器回归测试（Chromium + WebKit）
   python -m pytest -q \
     tests/test_call_log.py \
     tests/test_call_log_api.py \
     tests/test_call_log_browser.py \
     tests/test_call_log_fullstack.py \
     tests/test_ui_redesign.py
   ```

3. **建议新增专项回归断言**
   - `test_filter_change_preserves_queue_panel_and_search_focus`：验证切换筛选或输入搜索时 `#operation-queue` DOM 实例引用不变（不触发二次 `/api/operations/queue` 请求），且 `#audit-query` 焦点与光标位置不丢失。
   - `test_sse_operation_terminal_event_reconciles_both_queue_and_list_under_502`：模拟 SSE 推送 `succeeded` 后紧跟瞬态 `HTTP 502`，断言顶部活动条与下方调用行同步收敛至「已完成」，不残留幽灵「执行中」。
   - `test_queue_endpoint_constant_query_count`：压测 50 条等待/运行混合操作，断言 `/api/operations/queue` 执行的 SQL 语句数 `<= 4`（杜绝 `N+1` 回退）。
   - `test_sse_high_frequency_output_coalesces_without_global_resync`：模拟单操作每秒发送 200 帧 `output` 事件，断言 Watcher 不触发 `event: resync`，且折叠态审计页不发起多余的 `/api/call-log` 请求。
