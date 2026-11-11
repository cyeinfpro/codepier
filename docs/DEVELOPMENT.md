# 开发、构建与验证

从 GitHub Release 下载源码开发时使用 `codepier-VERSION-source-full.zip`。`source.zip` 供旧面板更新器安装，省略了 8 个开发专用顶层文件，不能替代完整开发源码进行回归验收。

## 依赖与单一版本源

运行环境推荐 Python 3.13，前端构建使用 Node.js 24。六套 requirements 的 `.in` 是审阅直接依赖的入口，`.txt` 是包含传递依赖、版本和哈希的可安装锁文件。不要手工删去哈希来绕过安装失败。兼容性 SDK 使用独立环境，避免污染 Hub 依赖。已有 `.venv` 不代表其中的包与当前锁文件一致；正式验证应在新建隔离环境按哈希安装并记录解释器/依赖版本，不覆盖其他任务正在使用的环境。

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python scripts/check_dependencies.py
python3.13 -m venv .venv-compat
.venv-compat/bin/python -m pip install --require-hashes -r requirements-compat.txt
.venv/bin/python -m playwright install chromium webkit
npm --prefix web/mcp-apps ci --ignore-scripts
```

Linux 还需 Playwright 对应系统依赖，可在隔离 CI 运行 install --with-deps。Python 下载缓存、npm 包缓存和浏览器二进制可以缓存；虚拟环境不跨平台复用。Mac 的 Compose 检查支持 docker compose 和独立 docker-compose，不要求 Docker daemon。

依赖更新时修改 `.in`，显式运行 `python scripts/lock_requirements.py --exclude-newer YYYY-MM-DD`，审阅六份锁文件，再在新环境安装并测试。解析只允许 wheel，不执行源代码发行包的构建脚本。`--check` 仅对比，不覆盖。版本源是 `shared/util.py` 的 VERSION；RELEASE.json、README 当前版本、Compose 与 k3s 清单镜像、支持分支与生成资源由发布检查核对。LOCAL_RELEASE.json 仅是本地历史记录，不是当前源码或部署状态的权威。

2026-10-12 的依赖维护保留原有完整依赖图，仅合入已审查的 Dependabot 固定版本与哈希。受影响锁文件分别记录基础解析截止日和后续 PR 更新来源，不把它们描述为按旧截止日重新解析的结果。后续需要重新解析整张依赖图时，仍须显式提供新的 `--exclude-newer` 日期并逐项审阅，不能为更新注释而引入未审阅的新版本。

2026-10-19 的依赖维护使用明确的解析截止日 `2026-10-19`，将 Pydantic 与其精确匹配的 Core 一同更新。开发与独立 SDK 的 `.in` 显式保留本次无关的既有传递版本和平台条件，避免解析器顺带升级其他依赖；六套锁文件继续由同一脚本生成并使用相同截止日执行 `--check`。后续解除这些保守固定版本时，应单独审查对应依赖的变化。

Playwright 1.64.1 在本机 macOS 26.3.1 / arm64 的默认 Chromium headless shell 上，即使空白合成页面也不推进 requestAnimationFrame，普通点击稳定性等待超时。浏览器验收通过 `tests/browser_launch.py` 显式选项策略：仅 macOS、Chromium、默认无头且未指定 channel/executable_path 时选择官方 `channel="chromium"` 完整浏览器新无头模式。Linux、Windows、WebKit、显式浏览器及有头选择原样保留。此策略不用于运行时，不关闭动画、actionability 或安全检查；移动元素仍须等待稳定。新增浏览器测试也应使用该 helper（包括 persistent context），不要全局猴子补丁或强制点击。参考 https://playwright.dev/docs/browsers#chromium-new-headless-mode 。嵌套 details 的 visibility 兼容修复和既有回归断言继续保留。

## 前端构建与格式

```bash
npm --prefix web/mcp-apps run format:check
npm --prefix web/mcp-apps run build
.venv/bin/python scripts/build_integration_assets.py
.venv/bin/python scripts/build_web_assets.py
.venv/bin/python scripts/build_web_assets.py --check
```

主动排版使用 npm run format。ES Module 公共层在 web/core，已有功能通过生成的 CP bundle 渐进接入；不要手工编辑 bundle.js、index.html 或 assets-manifest.json。页面模板 index.source.html 不手写版本号。修改任何入口资源后重新构建并校验哈希，否则启动或发布检查应失败，而不是发布混合版本资源。

协作房间源码在 `web/collaboration-room/*.mjs`，共享视图状态和生命周期集中于 `index.mjs`。聊天源码在 `web/chat/*.js`，按 `manifest.json` 顺序组合并保留现有全局接口；两个 `styles/manifest.json` 维持 CSS 层叠顺序。修改源码后运行 `npm --prefix web/mcp-apps run build`，再生成面板资源清单。不要直接编辑生成的 `web/collaboration-room.js`、`web/chat.js` 或对应的两个 CSS 文件；`node web/mcp-apps/build-room.mjs --check` 验证生成物一致性。

文案检查 `node scripts/check-ui-copy.mjs --check` 阻止明确的连续否定、装饰英文等问题；`--audit` 另列操作记录、权限、设备与目录用词供场景检查。工程诊断、真实协议名称及容量单位可以保留，不把模式命中数当成产品缺陷数。

## UI 设计系统约定

颜色、字号、字重、圆角、浮层层级、动效与阴影的真值在 `web/tokens.css`。组件只引用语义 token；中文辅助文字至少 12px，输入框保持 16px，11px 仅允许明确的等宽数字或键盘标记。全局 z-index 使用 `--ui-z-*`，组件内部数值不得超过 9。

原生面板经 `CP.ui.showModalLayer()` / `closeModalLayer()` 打开与关闭，移除面板前同步归还 toast 与授权收件箱。二次确认使用 `confirmSheet()`，不得复用单槽 modal 销毁正在编辑的窗口。异步业务确认在等待前后复核会话、空间、页面、元素与业务代次；同步导航/退出守卫需先重构状态机，不能机械改成 Promise。

页内菜单放在非 `overflow:hidden/clip` 的祖先中；`uiMenu()` 处理视口边界翻转。模态入场只改变透明度，避免 transform 将 fixed 授权卡片的包含块变成面板。短屏验收除了没有页面双滚动，还须实际看见消息内容并保留独立滚动区。

`scripts/ui_design_audit.py` 同时报告原始漂移与语义指标。`ui_design_fallbacks.json` 仅保存 conversation/approval 独立加载所需的精确回退声明（文件、条件、选择器、属性、值和次数），不能扩大为整文件或任意 var() 豁免。新增回退必须审阅，移除回退同时删除旧条目；`tests/test_ui_design_guard.py` 的原始及语义基线只降不升。

```bash
python3 scripts/ui_design_audit.py --json
# 默认仅预览；应用前逐组件检查，不批量改整个前端。
python3 scripts/ui_token_codemod.py web/chat.css
.venv/bin/python -m pytest tests/test_ui_design_guard.py tests/test_ui_layers.py tests/test_ui_design_layout.py
```

CSS 覆盖报告只是保守候选，不是自动删除授权。合并样式必须保留各 owner 的加载顺序与条件，不能把原来最后加载的组件覆盖统一搬到最前面的全局文件。

## 分层测试

```bash
# 真实 Hub/Agent 黄金路径，不需要浏览器或真实模型。
.venv/bin/python -m pytest -q -m smoke
# 最短反馈：排除浏览器及多进程慢验收。
.venv/bin/python -m pytest -q -m 'not browser and not slow'
# 不启动浏览器，但仍包括多进程集成测试。
.venv/bin/python -m pytest -q -m 'not browser'
# 增量类型检查与全仓基础静态检查。
.venv/bin/python -m mypy
.venv/bin/python -m ruff check agent hub shared scripts tests
```

browser/integration/slow 由实际导入及 fixture 依赖分类，smoke 和独立 matrix case 显式标记。新增重型 fixture 时同步分类并验证收集。类型检查当前覆盖 9 个配置、回归规划、异步收尾、记录范围、保留策略及执行策略/运行结果合同文件，不声称全部历史业务代码已严格类型化。共享浏览器、登录和证据工具放在 tests/browser_support.py 等支持模块，不从 test_*.py 导入 fixture。

通过 CodePier 清洁模式运行这些测试时，默认临时目录可能位于 Agent 私有状态目录中；测试创建的合成项目会因此触发 `PROTECTED_ROOT`。应为本次测试命令选择私有状态目录之外的临时位置，保留产品路径保护。macOS / Linux 可使用 `TMPDIR=/tmp .venv/bin/python -m pytest -q -m smoke`；Windows 应在本次测试进程中将 `TEMP` / `TMP` 指向专用测试临时目录。不要修改 Agent 的全局环境或把状态目录加入项目授权。

清洁模式的 HOME 也可能看不到执行账号已安装的 Playwright 测试浏览器。可为本次测试命令设置 `PLAYWRIGHT_BROWSERS_PATH` 指向已安装的测试浏览器缓存，或在隔离环境安装锁定版本需要的浏览器；测试仍使用新建的临时上下文，不复用个人浏览器档案。

完整回归使用全新目录；workers 不超过可用 CPU，资源受限时可更低：

```bash
CODEPIER_LONG_BUDGET_TEST=1 .venv/bin/python scripts/check_full_regression.py \
  --output .work/regression-final --workers 4 --timeout 2400 --coverage
```

运行器先完整收集，再按模块隔离进程；独立主题/视口案例各有自己的进程、临时目录、截图和报告。不会为变绿重试失败用例。耗时上限用于识别卡死，不是跳过结果。执行期间不能改源码。任意失败、跳过、丢失阶段、重复阶段、非零进程退出或源码变化都会使 verified=false。

## CI 分片与完整证明

GitHub Linux 浏览器和 OIDC 验收前，archive helper 显式使用 `--mode direct-source`。该固定源策略只支持 Ubuntu 24.04 / noble / x86_64；仅改写活动 source 中精确的 `mirror+file:/etc/apt/apt-mirrors.txt`，要求官方 Ubuntu `Signed-By` keyring、合法 Types、noble suites 和 components，以及受支持的 source 字段/选项。普通 suites 改为 `https://archive.ubuntu.com/ubuntu`，独立 `noble-security` 改为 `https://security.ubuntu.com/ubuntu`；其余字节、签名密钥、第三方源和文件权限保留。混合 security/普通 suites、多 URI、未知字段/选项、身份、架构或文件 URI 在任何写入前失败。helper 不读取、信任或修改镜像列表，不宣称识别了 AWS 的未知格式；这是明确收窄到固定官方 HTTPS 源的策略，不是校验失败后的静默 fallback，也不再使用列表中的镜像优先级或故障切换。

helper 默认仍为 `strict-mirror`：保留精确 Azure URI 转换、已确认的 144-byte 官方三行列表及其 HTTPS 两行结果校验，未知列表继续失败。两种模式均先验证全部 source，再原子写入并核对；注释和禁用条目不触发镜像访问。CI 不用于生产源配置，不关闭 APT 验签；原 30 秒下载超时、2 次重试、15 分钟安装预算和全部测试门槛保持不变。

Linux/macOS 每个平台各自有六片：四个普通分片、一个真实资源准入专片（index 4）、一个真实长时进程专片（index 5）。长时专片保留原来的 10/30 分钟墙钟测试及同模块短时/取消测试，不缩短预算、不模拟时间、不跳过。所有分片从同一份经重复构建确认可复现的公开源码 ZIP 解包运行；每片仍保存完整收集清单、实际执行结果、coverage 和源码前后指纹。只有六片同源码、同收集集合、每例恰好执行一次且全部通过，聚合才是完整证明。不得把独立长测移到定时任务后仍称当前发布全量通过。

```bash
# 每片使用独立输出目录；CI 为不同平台/分片提供独立 runner。
CODEPIER_LONG_BUDGET_TEST=1 .venv/bin/python scripts/check_full_regression.py \
  --output .work/shard-0 --workers 2 --timeout 2400 \
  --shard-count 6 --shard-index 0 --resource-shard --coverage
# index 1–5 全部完成后合并；任一缺失、失败、跳过或重复都失败。
.venv/bin/python scripts/merge_regression.py \
  .work/shard-{0,1,2,3,4,5}/summary.json --coverage --output .work/complete/summary.json
```

CI 的 Linux 源码构建 job 与 macOS 的第 0 片分别保留本平台的构建、静态检查与依赖审计；其他普通片只建立各自的依赖环境、安装测试需要的构建依赖，不重复相同构建检查。资源与长时片不安装浏览器。pip/npm/浏览器仅复用下载缓存，不共享或上传虚拟环境。

日常推送和 PR 不触发 CodePier CI。需要检查时，在 GitHub 的 Actions → CodePier CI → Run workflow 中选分支和模式。默认 full 对选中的提交运行完整回归；release-source 只运行公开源码构建及验收。命令行可以用 gh workflow run ci.yml --ref main。两种模式各自管理并发，准备发版的源码作业不会取消正在执行的完整回归。正式 GitHub 发布见 [GitHub 发布清单](RELEASING.md)。

覆盖率采集包括 Python 子进程，保存 XML/JSON 和可合并的数据。diff coverage 仅报告新增/修改的可执行 Python 行、未测量文件和未覆盖行，不设置旧代码全仓 80% 门槛，也不把没有数据说成 100%。分片先合并覆盖数据，再计算整个变更的覆盖率。

Windows CI 运行明确选出的核心 pytest 文件、真实 PowerShell 参数行为和临时计划任务恢复验收，不只是解析脚本文本。Windows 核心子集与 Linux/macOS 全量证明分别报告；本机未执行的远端作业不能写成已通过。临时任务验证不重启电脑，不等于完成未登录启动的目标机演练。

## Hub 历史维护

默认保留终态正文 30 天、审计 365 天；维护不会在启动或升级时自动执行。先用现有 Hub 数据目录预览候选与引用保护来源，再按同一策略执行：

```bash
.venv/bin/python scripts/maintain_history.py --data-dir /path/to/hub-data
.venv/bin/python scripts/maintain_history.py --data-dir /path/to/hub-data --apply
```

预览只读打开已有数据库，缺失路径直接报错，不创建新状态或运行迁移。`--operation-days` 最低 30、`--audit-days` 最低 365，`--batch` 为 1–500；一次最多处理这个批量，重复执行可继续推进。每次执行仍在事务内重新检查引用，预览不是对之后数据状态的承诺。JSON 报告包含候选记录、正文/归档字节、保护总数及原因计数；多个保护原因可重叠。身份、指纹、幂等键、墓碑、未决操作、冻结结果和强引用保留；不会删除附件/备份或执行 VACUUM。使用分离状态目录的部署沿用既有 `HUB_STATE_DIR` 配置。

## 证据、归档和隐私

测试目录包含日志、截图、合成凭据、浏览器档案或数据库，即使是 fixture 也不应整目录公开上传。CI 只保留报告、输出和经过选择的截图，排除 tmp、cache、认证状态；覆盖数据作为显式选择的隐藏文件上传。GitHub artifact 保留 14 天。

长期 docs/evidence 默认不进入源码包。`scripts/archive_evidence.py` 默认只列出超过保留期的候选；明确指定 --archive 后才写入私有归档，绝不自动删除原始证据。先验证归档摘要、恢复可读性和业务保留要求，再由所有者决定是否清理。该工具不是 Hub 数据库、密钥或原生会话的备份工具。

当前指南使用无日期的大写下划线文件名。历史审查或验收使用日期后缀，重复历史版本保留在忽略的 docs/history；旧公开入口可以保留简短跳转，不能存在两份相互冲突的安全或会话规范。`.gitignore` 不再整体忽略新文档，但明确保留既有私人记录的忽略规则。公开包仍需独立白名单和全部相对链接检查。

## 验证边界

本地测试以临时 Hub、Agent、SQLite、真实 Chromium/WebKit 和确定性原生 CLI 替身为主，不使用个人配置，不自动调用付费模型。真实操作系统服务、容器构建、数据迁移、外部账户和本机桌面需按目标平台独立验收。源码测试、提交推送、GitHub CI、正式 Release 和部署是不同状态，不能互相替代。详细发布步骤见 [RELEASING](RELEASING.md)。
