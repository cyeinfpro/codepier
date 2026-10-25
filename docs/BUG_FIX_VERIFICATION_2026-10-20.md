# 2026-10-20 Bug 报告修复与验证记录

## 范围与交付状态

针对仓库根目录 `BUG_REPORT_2026-10-20.md` 的全部 23 个条目逐项复核。原报告混合了可复现缺陷、风险猜测、已有防护和设计建议；本次按实际调用链修复确认的缺陷，并为已有安全边界补充回归验证。

- 项目：CodePier `MCP`，仓库根目录 `.`。
- 起始分支：`main`；起始 HEAD：`2eb086c9ac3650d901b92c07fdc219eaee335317`。
- 原报告 SHA-256：`aa7ea391121d5fccc6c77a44f30b11f538bc2271bbafb47d1a7f0fb2a9a149c5`。
- 起始已跟踪文件无改动。原有报告及五份未跟踪设计/实施文档（共六个未跟踪文件）予以保留。
- 认证、运行时和前端并行推进，公共 HTTP 与进程日志单独实施，统一集成验证。

**已完成报告范围内确认缺陷的代码修复、构建和验证。最终通过测试合计 617 项，其中本次新增 66 项。修复保留在 main 工作区，尚未提交、推送或部署。**

## 23 项逐条结论

| 编号 | 报告主题 | 处理结论与依据 |
| --- | --- | --- |
| 1 | Session Token 校验不足 | 未复现认证绕过；原报告漏计了已有的账号、epoch 和身份检查。`Auth.session` 同时检查账号启用状态、会话安全记录、撤销 epoch 和外部身份状态。新增过期、撤销、停用、缺失安全记录测试。 |
| 2 | 动态查询 SQL 注入 | 查询列为代码内固定两种投影，操作 ID 始终参数化；仍通过实时记录和项目权限校验。补注入型 ID、状态投影和越权回归，不增加无必要的动态 SQL 框架。 |
| 3 | 密码重放/暴力尝试 | 修复仅按 IP 限流的实际缺口。新增账号摘要维度的 300 秒窗口，每账号最多准入 8 次；以 `BEGIN IMMEDIATE` 原子预占，支持多 Store 连接、数据库重开，并限制有效账号尝试记录为 4096。拒绝请求不延长窗口。 |
| 4 | CSRF Token 随机性 | 原弱随机猜测不成立。`shared.crypto.token` 使用 `secrets.token_urlsafe`，默认 32 字节；写请求已有同源检查、会话关联及常量时间比较。新增跨会话 token 和跨源拒绝测试。 |
| 5 | 连接竞态 | 原同步 detach fence 已按连接身份保护；确认并修复发送等待授权期间旧连接失效后仍可发送的异步竞态。授权等待结束后再次检查连接失效状态，禁止旧通道发送和推进序列号。 |
| 6 | 前端输出/XSS | 检查文本、属性、Markdown 和链接输出的编码边界，保留现有安全输出方式。具体测试见下方前端验证记录。 |
| 7 | 路径遍历 | 原“仅前端验证”的描述不成立。`FileEngine` 服务端校验路径片段、实际根目录、符号链接和受保护目录；启动 native CLI 前验证 cwd。新增绝对路径、父路径、分隔符、符号链接逃逸及百分号字面文件名测试。 |
| 8 | 未验证重定向 | 报告目标为应用内部导航，未构成可控外部 URL 重定向。保留现有固定页面路由。 |
| 9 | 敏感操作二次验证 | 报告涉及的会话记录删除已有状态检查、明确确认及实时视图校验；文件变更另有权限、CSRF、备份和 SHA 冲突控制。额外重复输入密码属于交互设计建议，不作为已存在漏洞直接加入。 |
| 10 | 上传校验 | 现有 native 上传已经限制单文件、预留总量、分块、最终 SHA-256、归属与私有存储；文件名不能决定实际写入位置。补异常大小、哈希不符、跨用户采用、恶意显示名与恢复回归。开发项目允许源代码等通用文件类型，不引入不适用的 MIME 白名单。 |
| 11 | EventSource 清理 | 修复离开/切换聊天时渲染异常会中断清理的问题：先关闭流连接，退出清理放在 `finally`，避免异常留下旧连接与监听器。 |
| 12 | 失败信息泄露 | 失败登录审计不再写入原始用户名输入，改为带标识的 SHA-256 摘要，保留事件关联。该失败登录审计不记录密码；本次涉及的配置和重连诊断不再直接输出原始异常文本。 |
| 13 | 保存并发控制 | 已有同步占位锁、禁用按钮、SHA 冲突校验和视图代次保护。通过现有异步恢复测试验证迟到响应、并行输入和冲突行为。 |
| 14 | 日志级别配置 | 新增 `CODEPIER_LOG_LEVEL`，支持 debug/info/warning/error/critical。Hub 和 Agent 启动时配置 `codepier` 日志命名空间，handler 幂等；Hub 同时将选定级别传给 Uvicorn，不全局调整其他库或 root logger。同步配置示例和 Compose。 |
| 15 | WebSocket 限流 | 复核已有串行 await 背压、加密序列检查、接收大小、空闲超时、发送超时及业务域容量。补连接发送与清理回归；不对合法持续输出施加任意低 QPS 断链策略。 |
| 16 | 魔法数字 | 将本次涉及的消息、附件、上传分块、账号尝试、HTTP 缓冲及发现分页限制改为具名常量，保持协议限额一致。 |
| 17 | 异常处理 | HTTP 通用 500 复用统一安全响应策略，保留 MCP 错误追踪字段且不反射异常正文；日志诊断和前端退出清理按各自异常边界处理。 |
| 18 | 输入长度 | 前端按 UTF-8 字节数与服务端 64 KiB 消息限制对齐；中文/emoji 超限可即时提示并禁发，发送入口仍保留校验。 |
| 19 | 项目分页 | 修复 MCP 发现接口先全量加载、观察在线状态、排序后才分页的问题。改为每次最多 100 个候选的数据库 keyset 遍历，SQL 先限制空间和凭据项目范围，角色授权先于计数与分页，仅对返回页计算在线状态。旧内部完整列表接口保留兼容。 |
| 20 | DOM/长会话性能 | 避免每个流式帧扫描全部历史消息和审批项；渲染访问限定到当前显示窗口，审批状态用增量索引维护。保留现有逐帧合并和消息 DOM 节点复用。 |
| 21 | HTTP 安全头 | 修复通用策略覆盖附件专用 sandbox CSP、异常 500 缺少安全头、HTTPS 缺少 HSTS。保留更严格的路由 CSP，通过可信 ASGI scheme 判断 HTTPS，保留私有 no-store 指令。 |
| 22 | CORS | 未发现允许任意来源的 CORS 配置；面板会话写请求已有 Origin/Fetch Metadata 约束。新增跨源普通请求及预检不获得跨源许可的测试。 |
| 23 | 速率/容量 | 保留现有执行任务、native、上传、OAuth 等业务域限额，补 HTTP 接收资源缺口：每进程最多 256 个正在读 body 的请求、64 MiB 保留原始 body 字节预算、累计 30 秒读超时。容量不足返回 429，超时返回 408，进入业务处理前明确 `admitted:false`。 |

## 关键实现决定

### 认证与迁移

新增 `hub/auth_schema.py` 的增量迁移，新建账号摘要尝试表和索引。保留原 `login_attempts(ip, at)` 两列接口及 OAuth 的 60 秒/30 次窗口；旧 IP 记录仍按原 900 秒清理。新表仅记录账号摘要和时间，不迁移或重建用户、会话、授权或加密密钥。

原有限流同样在密码校验前预占 CPU 容量，本次增加持久化账号准入，不改变取消等待期间 scrypt 容量仍由实际 worker 持有的规则。每条准入记录 300 秒后不再计入账号预算；拒绝探测不追加记录，避免用被拒绝请求延长已有记录的有效期。

失败输入的摘要用于日志关联，不应描述为不可逆匿名化。真实用户名的正常成功登录审计保留现有可审计语义。

### HTTP、上传和长连接

安全头函数同时用于普通响应和框架最外层 500 处理，专用附件 `sandbox; default-src 'none'` 不能被面板默认 CSP 覆盖。HSTS 只按应用实际观察到的 HTTPS scheme 设置；未经信任的客户端 `X-Forwarded-Proto` 不构成依据。

HTTP body 预算按读取阶段与业务处理阶段分别管理。读完后释放接收槽位；已缓存字节的预算一直保留到下游请求处理结束。断开、超时、取消和异常都有 `finally` 释放，空 body 的 SSE 不长期占用接收槽位。64 MiB 是累计原始请求体预算，不是整个 Python 进程 RSS 上限。

上传和操作执行已各有独立限额。新增 HTTP 预算补齐接收阶段资源约束，不代替业务授权或任务调度，也不改变原有幂等回执和恢复机制。

### 分页与权限

项目发现分页保持现有 `query`、`offset`、`limit`、`total`、`next_offset` 和连接观察字段。任何过滤、计数与页截取都必须在当前权限下执行；不会通过总数泄露被拒绝项目。

为了返回精确 `total` 并保持 Unicode 查询语义，发现接口仍需检查符合凭据范围的候选项目；改进目标是限制单批候选内存和避免页面外在线状态查询，不宣称总扫描复杂度已经变成常数。面板与内部依赖完整列表的调用保持原接口。

## 验证记录

### 最终通过结果

| 验证组 | 结果 | 范围与证据 |
| --- | --- | --- |
| 后端综合回归，22 个模块 | **519 通过，0 失败，0 错误，0 跳过；59.56 秒** | 认证、迁移、OAuth/OIDC、HTTP、安全头、请求审计、文件/上传恢复、项目发现、连接、IAM 及 Store。操作 `07d2ded463394faf9ae7c628a8bf5d79`。 |
| 前端综合回归，9 个模块 | **94 通过，0 失败，0 错误，0 跳过；65.22 秒** | 29 项逻辑/Node 行为测试，65 项真实 Chromium/WebKit 场景，包含生产页面启动、草稿与设置恢复、折叠状态、移动布局和安全文本输出。操作 `e3a34cfbbb2745f6a8df778a216c92d2`。 |
| 新提示的 CSS 可见性与恢复回归 | **4 通过，0 失败，0 错误，0 跳过；2.79 秒** | Chromium/WebKit × 320/1440 宽度，63,000 字节正常提示、65,538 字节禁发、草稿保留、缩短恢复及无水平溢出。操作 `0dd402c1996f4ba0ad9a6e5a8e80a827`。 |
| Ruff | 通过 | `ruff check --no-cache agent hub shared scripts tests`；涵盖新增回归文件。 |
| Mypy | 通过 | 现有配置覆盖的 3 个源文件通过；不扩大表述为全仓完整类型验证。 |
| 前端格式检查 | 通过 | `npm --prefix web/mcp-apps run format:check`。 |
| 构建与资源一致性 | 通过 | MCP Apps、集成资源和面板资源重新构建；`build_web_assets.py --check` 返回无过期资源。 |
| Git 差异检查 | 通过 | `git diff --check`；分支和 HEAD 保持起始状态。 |

上述最终通过组去重合计 **617 项**。本次新增 **66 项回归，分布在 9 个新测试模块**。早期的 131 项认证组合、200 项文件组合等已被综合组覆盖，不重复计入总数。

后端综合组出现 1 条现有 Starlette/httpx TestClient 弃用提醒，不影响测试结果。

### 修复前后对照

| 场景 | 修复前证据 | 修复后行为 |
| --- | --- | --- |
| 同账号更换 IP 继续试密码 | 两个真实/未知用户名场景，第 9 次仍返回密码失败而非限流；操作 `9a517237ccab4421af541e88bf2c319a`。 | 返回 `LOGIN_THROTTLED`，同账号共享预算；多 Store 16 路竞争只准入 8 次。 |
| 授权等待期间连接被替换 | 旧连接继续发送，预期 ConnectionError 未发生；操作 `d5c2ade877744fed91ce4f9f8b541103`。 | 旧连接不发送，`SecureChannel.sent` 保持零；合法 128 个并发 ACK 顺序完整。 |
| 项目发现分页 | 一次读取 267 条完整项目记录才切页；同一操作中的分页回归失败。 | 分批读取 100/100/67 条候选；返回 7 条页面记录时，同设备在线状态只观察一次。 |
| HTTP 安全头 | 附件 sandbox CSP、HTTPS/500 三个缺口由新增测试复现；操作 `72ce2c272f444a0d9886c9f955b386d8`。 | 保留路由专用 CSP；HTTPS 响应及 500 带一致安全头。 |
| 聊天退出、长历史与 Unicode | 退出异常遗留连接、无会话未关旧连接、20,000 条历史扫描、Unicode 超限禁发等场景未通过；操作 `db9fc9e4aaea4da6b75aa3c5b19155d2`。 | 对应 7 个真实函数 Node 场景通过，原有折叠节点状态继续保持。 |
| 新字节提示的父容器隐藏 | 真实 CSS 中“安静状态行”规则隐藏了提示的祖先，新增可见性测试复现；操作 `af6382ddae3248beaea4bfe915de18f2`。 | CSS 仅在没有可见字节提示时隐藏安静状态行；四个真实浏览器/宽度组合通过。 |

前端 Node 测试最终版本使用外部行为断言；修复前的初版还含两个对新增辅助结构的断言，因此不把初版全部失败数量等同于七个独立漏洞。

### 测试环境与构建处理

测试使用现有 `.venv-hardening`（Python 3.13）与 Node 22。临时目录显式放在 `.work/bugfix-20261020/`，避免清洁运行环境的默认缓存目录被正确的 `PROTECTED_ROOT` 防线拒绝。没有为通过测试放宽项目路径规则。

首次真实浏览器启动失败来自清洁环境的独立 HOME 未包含 Playwright 二进制缓存。确认本机现有版本后，只为测试命令设置 `PLAYWRIGHT_BROWSERS_PATH`，复用 Chromium 1243 和 WebKit 2359；随后所有相关测试通过，不下载或使用个人浏览器配置。

构建复核发现本地安装的 Prettier 为 3.9.9，仓库 manifest/lock 均为 3.9.10。执行 `npm ci --ignore-scripts --no-audit --no-fund` 按现有锁文件对齐依赖后重新构建，避免生成第三方说明的无关版本倒退；没有更改依赖声明或锁文件。

### 可复现命令

在项目目录执行。再次运行时给 `--basetemp` 选择新的目录名，避免清理另一正在执行的测试的临时数据。

后端综合：

```bash
.venv-hardening/bin/python -m pytest -q \
  tests/test_bug_report_auth_20261020.py \
  tests/test_bug_report_body_budget_20261020.py \
  tests/test_bug_report_file_boundaries_20261020.py \
  tests/test_bug_report_http_20261020.py \
  tests/test_bug_report_logging_20261020.py \
  tests/test_bug_report_runtime_20261020.py \
  tests/test_audit_api.py \
  tests/test_audit_store.py \
  tests/test_audit_runtime.py \
  tests/test_oidc_integration.py \
  tests/test_oidc_bootstrap.py \
  tests/test_native_upload_recovery.py \
  tests/test_incoming_uploads.py \
  tests/test_unit.py \
  tests/test_optimization_store.py \
  tests/test_mcp_request_audit.py \
  tests/test_project_view_foundation.py \
  tests/test_project_view_integration.py \
  tests/test_project_view_owner_context_integration.py \
  tests/test_runtime_hardening.py \
  tests/test_iam_hardening.py \
  tests/test_iam_final_regressions.py \
  --basetemp .work/bugfix-20261020/integration-backend-01 \
  -o cache_dir=.work/bugfix-20261020/cache-final_backend \
  --junitxml=.work/bugfix-20261020/backend-results.xml
```

前端综合：

```bash
PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright" \
.venv-hardening/bin/python -m pytest -q \
  tests/test_bug_report_frontend_security_browser_20261020.py \
  tests/test_bug_report_frontend_20261020.py \
  tests/test_audit_web.py \
  tests/test_tool_receipt_recovery.py \
  tests/test_chat_browser.py \
  tests/test_chat_window_browser.py \
  tests/test_chat_complete_browser.py \
  tests/test_chat_webkit_browser.py \
  tests/test_chat_process_browser.py \
  --basetemp .work/bugfix-20261020/integration-frontend-02 \
  -o cache_dir=.work/bugfix-20261020/cache-final-frontend \
  --junitxml=.work/bugfix-20261020/frontend-results-final.xml
```

计数提示的真实可见性：

```bash
PLAYWRIGHT_BROWSERS_PATH="$HOME/Library/Caches/ms-playwright" \
.venv-hardening/bin/python -m pytest -q \
  tests/test_bug_report_frontend_counter_browser_20261020.py \
  --basetemp .work/bugfix-20261020/counter-final-01 \
  --junitxml=.work/bugfix-20261020/counter-results.xml
```

静态与构建：

```bash
.venv-hardening/bin/python -m ruff check --no-cache agent hub shared scripts tests
.venv-hardening/bin/python -m mypy --cache-dir .work/bugfix-20261020/final-mypy-cache
npm --prefix web/mcp-apps run format:check
npm --prefix web/mcp-apps run build
.venv-hardening/bin/python scripts/build_integration_assets.py
.venv-hardening/bin/python scripts/build_web_assets.py
.venv-hardening/bin/python scripts/build_web_assets.py --check
git diff --check
```

### 文件与产物

| 范围 | 主要文件 |
| --- | --- |
| 认证与迁移 | `hub/auth.py`、`hub/auth_schema.py`、`hub/store.py` |
| HTTP 与进程日志 | `hub/http.py`、`shared/process_logging.py`、`hub/config.py`、`hub/__main__.py`、`agent/__main__.py`、`agent/runner.py`、`.env.example`、`compose.yml` |
| 连接与项目发现 | `hub/runtime.py`、`hub/project_views.py` |
| 前端 | `web/chat.js`、`web/chat-chrome.js`、`web/chat.css`，以及重建的 `web/index.html`、`web/assets-manifest.json` |
| 回归 | `tests/test_bug_report_*_20261020.py` 九个新模块；`tests/test_project_view_foundation.py` 仅更新分页接口替身 |
| 配置说明 | 既有本地 `docs/CONFIGURATION.md` 同步说明日志和 HTTP 预算；该文件预先被 Git 忽略，本记录也完整保留关键说明。 |
| 原始测试记录 | `.work/bugfix-20261020/backend-results.xml`、`frontend-results-final.xml`、`counter-results.xml` |
| 移动端截图 | `docs/evidence/bugfix-20261020/chat-counter-mobile.png`，已目视检查提示、输入框和发送按钮完整可见。 |

## 验证边界

本次验证针对报告范围内的认证、HTTP、文件安全、连接、发现接口与前端行为。浏览器测试使用确定性 HTTP/SSE 边界；native 文件测试使用临时目录和测试进程，不需要调用收费模型。未进行生产部署、服务重启、Git 提交或推送；未执行全平台发布认证，也不把本次测试解释为整个仓库没有其他缺陷的保证。
