# 开发、构建与验证

从 GitHub Release 下载源码开发时使用 `codepier-VERSION-source-full.zip`。`source.zip` 供旧面板更新器安装，省略了 8 个开发专用顶层文件，不能替代完整开发源码进行回归验收。

## 依赖与单一版本源

运行环境推荐 Python 3.13，前端构建使用 Node.js 24。六套 requirements 的 `.in` 是审阅直接依赖的入口，`.txt` 是包含传递依赖、版本和哈希的可安装锁文件。不要手工删去哈希来绕过安装失败。兼容性 SDK 使用独立环境，避免污染 Hub 依赖。

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
python3.13 -m venv .venv-compat
.venv-compat/bin/python -m pip install --require-hashes -r requirements-compat.txt
.venv/bin/python -m playwright install chromium webkit
npm --prefix web/mcp-apps ci --ignore-scripts
```

Linux 还需 Playwright 对应系统依赖，可在隔离 CI 运行 install --with-deps。Python 下载缓存、npm 包缓存和浏览器二进制可以缓存；虚拟环境不跨平台复用。Mac 的 Compose 检查支持 docker compose 和独立 docker-compose，不要求 Docker daemon。

依赖更新时修改 `.in`，显式运行 `python scripts/lock_requirements.py --exclude-newer YYYY-MM-DD`，审阅六份锁文件，再在新环境安装并测试。解析只允许 wheel，不执行源代码发行包的构建脚本。`--check` 仅对比，不覆盖。版本源是 `shared/util.py` 的 VERSION；RELEASE.json、README 当前版本、Compose 与 k3s 清单镜像、支持分支与生成资源由发布检查核对。LOCAL_RELEASE.json 仅是本地历史记录，不是当前源码或部署状态的权威。

2026-10-12 的依赖维护保留原有完整依赖图，仅合入已审查的 Dependabot 固定版本与哈希。受影响锁文件分别记录基础解析截止日和后续 PR 更新来源，不把它们描述为按旧截止日重新解析的结果。后续需要重新解析整张依赖图时，仍须显式提供新的 `--exclude-newer` 日期并逐项审阅，不能为更新注释而引入未审阅的新版本。

2026-10-19 的依赖维护使用明确的解析截止日 `2026-10-19`，将 Pydantic 与其精确匹配的 Core 一同更新。开发与独立 SDK 的 `.in` 显式保留本次无关的既有传递版本和平台条件，避免解析器顺带升级其他依赖；六套锁文件继续由同一脚本生成并使用相同截止日执行 `--check`。后续解除这些保守固定版本时，应单独审查对应依赖的变化。

本次保留 Playwright 1.63.0：1.64.0 所带 Chromium 在本机 macOS 26.3.1 / arm64 上，即使只加载无项目样式或脚本的按钮页面，`requestAnimationFrame` 仍不推进，正常点击等待稳定状态超时；相同主机和探针下 1.63.0 正常。升级应等待该执行环境的兼容性验收通过。嵌套 `details` 的关闭保护改用 `visibility: hidden`，已在两版 WebKit 验证展开、选择、折叠与重开，保留这项兼容修复和回归断言。

## 前端构建与格式

```bash
npm --prefix web/mcp-apps run format:check
npm --prefix web/mcp-apps run build
.venv/bin/python scripts/build_integration_assets.py
.venv/bin/python scripts/build_web_assets.py
.venv/bin/python scripts/build_web_assets.py --check
```

主动排版使用 npm run format。ES Module 公共层在 web/core，已有功能通过生成的 CP bundle 渐进接入；不要手工编辑 bundle.js、index.html 或 assets-manifest.json。页面模板 index.source.html 不手写版本号。修改任何入口资源后重新构建并校验哈希，否则启动或发布检查应失败，而不是发布混合版本资源。

## 分层测试

```bash
# 20 个真实 Hub/Agent 黄金路径，不需要浏览器或真实模型。
.venv/bin/python -m pytest -q -m smoke
# 最短反馈：排除浏览器及多进程慢验收。
.venv/bin/python -m pytest -q -m 'not browser and not slow'
# 不启动浏览器，但仍包括多进程集成测试。
.venv/bin/python -m pytest -q -m 'not browser'
# 增量类型检查与全仓基础静态检查。
.venv/bin/python -m mypy
.venv/bin/python -m ruff check agent hub shared scripts tests
```

browser/integration/slow 由实际导入及 fixture 依赖分类，smoke 和独立 matrix case 显式标记。新增重型 fixture 时同步分类并验证收集。类型检查当前覆盖配置与回归规划模块，不声称全部历史业务代码已严格类型化。共享浏览器、登录和证据工具放在 tests/browser_support.py 等支持模块，不从 test_*.py 导入 fixture。

完整回归使用全新目录；workers 不超过可用 CPU，资源受限时可更低：

```bash
CODEPIER_LONG_BUDGET_TEST=1 .venv/bin/python scripts/check_full_regression.py \
  --output .work/regression-final --workers 4 --timeout 2400 --coverage
```

运行器先完整收集，再按模块隔离进程；独立主题/视口案例各有自己的进程、临时目录、截图和报告。不会为变绿重试失败用例。耗时上限用于识别卡死，不是跳过结果。执行期间不能改源码。任意失败、跳过、丢失阶段、重复阶段、非零进程退出或源码变化都会使 verified=false。

## CI 分片与完整证明

GitHub Linux 浏览器测试前，独立 helper 仅把 `azure.archive.ubuntu.com/ubuntu` 的精确 HTTP/HTTPS URI 切换为 Ubuntu 官方 `https://archive.ubuntu.com/ubuntu`，避免 Azure 镜像持续低速占满安装预算。支持 deb822 与 legacy sources；保留签名密钥、suite、component、security 及第三方源，异常格式拒绝修改。此为显式的 CI 镜像策略，不用于生产或关闭 APT 验签；合成测试同时约束镜像选择与原下载超时 step。原 30 秒下载超时、2 次重试、15 分钟安装预算和全部测试门槛不变。 对 runner 新版 `mirror+file:/etc/apt/apt-mirrors.txt`，仅识别官方固定三行列表并移除 Azure `priority:1` 行，保留既有 HTTPS archive/security 行及优先级，避免重复 URI；仅访问固定文件，不跟随任意 file URI，非预期内容失败而不继续安装。

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

候选完整通过后，main 的“Validate same-commit promotion”可以只读验证同一仓库、同一完整 SHA、同一 workflow、最新有效成功 job 集合、两平台完整证明、coverage、公开 ZIP 和 manifest 的精确哈希，再复用候选证据。已失败/未结束的后续同 workflow 验证、过期或缺失 artifact、不同源码/workflow/归档都不允许复用；读取失败回退全量。复用不伪造本轮测试执行：Release gate 明确报告来源 run，受复用替代的 job 显示 skipped。首次候选、PR 和没有合格证据的 main 仍跑完整六片。

覆盖率采集包括 Python 子进程，保存 XML/JSON 和可合并的数据。diff coverage 仅报告新增/修改的可执行 Python 行、未测量文件和未覆盖行，不设置旧代码全仓 80% 门槛，也不把没有数据说成 100%。分片先合并覆盖数据，再计算整个变更的覆盖率。

Windows CI 运行明确选出的核心 pytest 文件、真实 PowerShell 参数行为和临时计划任务恢复验收，不只是解析脚本文本。Windows 核心子集与 Linux/macOS 全量证明分别报告；本机未执行的远端作业不能写成已通过。临时任务验证不重启电脑，不等于完成未登录启动的目标机演练。

## 证据、归档和隐私

测试目录包含日志、截图、合成凭据、浏览器档案或数据库，即使是 fixture 也不应整目录公开上传。CI 只保留报告、输出和经过选择的截图，排除 tmp、cache、认证状态；覆盖数据作为显式选择的隐藏文件上传。GitHub artifact 保留 14 天。

长期 docs/evidence 默认不进入源码包。`scripts/archive_evidence.py` 默认只列出超过保留期的候选；明确指定 --archive 后才写入私有归档，绝不自动删除原始证据。先验证归档摘要、恢复可读性和业务保留要求，再由所有者决定是否清理。该工具不是 Hub 数据库、密钥或原生会话的备份工具。

当前指南使用无日期的大写下划线文件名。历史审查或验收使用日期后缀，重复历史版本保留在忽略的 docs/history；旧公开入口可以保留简短跳转，不能存在两份相互冲突的安全或会话规范。`.gitignore` 不再整体忽略新文档，但明确保留既有私人记录的忽略规则。公开包仍需独立白名单和全部相对链接检查。

## 验证边界

本地测试以临时 Hub、Agent、SQLite、真实 Chromium/WebKit 和确定性原生 CLI 替身为主，不使用个人配置，不自动调用付费模型。真实操作系统服务、容器构建、数据迁移、外部账户和本机桌面需按目标平台独立验收。源码测试、提交推送、GitHub CI、正式 Release 和部署是不同状态，不能互相替代。详细发布步骤见 [RELEASING](RELEASING.md)。
