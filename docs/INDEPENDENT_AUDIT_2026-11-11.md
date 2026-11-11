# CodePier 独立审查：损坏、不一致、低效与可清理项 · 2026-11-11

本报告最初只记录发现，与同日的 `docs/PROJECT_AUDIT_2026-11-11.md` 互补。用户随后授权全部处理；以下第 0 节记录修复结果，第 1 节起保留原始审查快照。

## 0. 修复处置记录（2026-11-11）

本节是原始审查后的处置结果。A1–A7、B1–B11、C1–C8、D1–D8 共 34 项均已逐项核对；真实缺陷已修复，需要兼容或仍有使用价值的项目保留并说明理由。原始发现保留在后文，描述的是修复前快照，不代表当前仍有同样缺陷。

修改在原有 `main` 工作区上继续完成，保留此前修复；本轮没有提交、推送或部署，也没有在生产数据库上执行迁移。代码格式化使差异涉及较多文件，功能改动与验证证据见下表。

### 0.1 损坏项

| 项目 | 处置 | 当前行为与证据 |
| --- | --- | --- |
| A1 | 已修复 | `safe_summary` 统一按敏感键名处理，再应用上传、原生文件、浏览器和命令的专用规则；同级与嵌套敏感字段不再绕过脱敏，原始参数不被修改。新增回归覆盖两种报告中的泄漏形状。 |
| A2 | 已核实不成立 | 用 `git ls-remote --tags origin` 核实远端 `v1.33.12` 已存在，指向 `9dcbc539b8e24d222d7164cf0e3ec9ce8664b457`。保留有效安装版本；没有把工作区的 `RELEASE.json` 伪改为已发布。本地缺少标签不能证明远端不存在。 |
| A3 | 已修复 | 四个被直接调用的 shell 脚本均恢复可执行位；源码 ZIP 保留执行模式。 |
| A4 | 已修复 | 宿主更新脚本保持 Python 3.9 下限，移除不兼容的 `zip(strict=...)`，单独设置 Ruff 目标版本；增加宿主脚本语法/API检查、独立导入冒烟与 PR Python 3.9 作业。已实际使用 Python 3.9.25 验证 13 个脚本。 |
| A5 | 已修复 | 删除条件表达式的重复分支，明确检查原生会话标识不变。 |
| A6 | 已修复 | `maintain_history.py --encrypt-legacy` 默认只读预览，`--apply` 才按批次执行可续跑加密；与历史裁剪互斥，预览不会初始化数据库、创建密钥或运行 schema 迁移。补充运维命令与回归测试。 |
| A7 | 已修复 | Agent rollout 停止失败后，OIDC 的独立清理仍会执行；其余资源继续按顺序释放。 |

### 0.2 配置、分层与文档

| 项目 | 处置 | 当前行为与证据 |
| --- | --- | --- |
| B1 | 已修复 | Compose 增加 18 个生产环境变量透传，包括 12 个 OIDC 设置、来源限制与协作回滚开关。第 13 个 OIDC 变量 `CODEPIER_OIDC_INSECURE_TEST_LOOPBACK` 是测试专用开关，不作为生产部署选项开放。 |
| B2 | 已修复 | 示例环境配置补齐缺失设置，CPU 默认值统一为 1.5，说明本地 stdio 桥接的 profile/retry 设置，更新协作指南链接和过时注释。新增 22 项部署设置与时区/CPU 默认值一致性检查。 |
| B3 | 已修复 | 新增 PR 自动轻量检查；完整 CI 保留手动执行。手动覆盖率基线支持显式引用，默认寻找前一个可达标签；删除永真 PR 条件与失效开发分支触发器，补齐 action 固定版本注释。 |
| B4 | 已修复 | Agent 运行与手工安装统一 Python 3.13，宿主更新器单独维持已实测的 Python 3.9 支持；文档与安装检查同步。 |
| B5 | 已修复 | POSIX/Windows 手工入口都使用 `install_agent.py --source`，统一受管安装目录与后台服务生命周期，保留已有配置和设备身份；安装器按平台选择 venv、uv 与服务路径，增加两个平台的契约测试。 |
| B6 | 已修复 | HTTPS 下载运输层移入 `shared/file_download.py`；更新包解析移入 `shared/release_source.py`，Hub 不再导入 Agent 下载模块或脚本模块。镜像只复制运行和 Agent 分发必要的 scripts/deploy 文件；Hub 锁文件移除 Agent 专用三项依赖，开发锁仍覆盖 Agent。独立 Hub 环境验证健康检查与 Agent 包生成均成功。 |
| B7 | 已处理 | 两个安装/生命周期脚本都需要在源码缺失或替换期间独立运行，因此保留内嵌副本；以共享模块为唯一源，用生成器同步函数并在 CI 检查漂移，避免三处人工维护。 |
| B8 | 已修复 | README 指向当前协作指南，版本变化交由 CHANGELOG；文件入站三篇文档明确各自职责，移除旧版本号和失效开关说明。导航整合公开指南，内部设计/审查记录在 `docs/REPOSITORY_INDEX.md` 中索引，避免将内部资料链接加入公开发布包。 |
| B9 | 已修复 | 两个仓库内 SKILL.md 更新为当前 query/act 工作流、密钥环备份与分层验证方式，去除过时 schema、固定工具数量和旧工具名。 |
| B10 | 已修复 | 统一 Hub 服务描述、FastAPI 标题与临时文件前缀；Python 默认时区集中定义，部署模板保留声明式值并由一致性检查约束。 |
| B11 | 已修复 | 启用生产代码 F401，移除无用导入与失效 noqa；兼容导出显式标注。mypy 改为检查实际使用的历史保留模块。回归发现并恢复 `iam.read_scope` 导出，随后外部导入引用扫描无遗漏。 |

### 0.3 效率、发布与占用

| 项目 | 处置 | 当前行为与证据 |
| --- | --- | --- |
| C1 | 已修复 | 协作中心、VPS、MCP 网关的 CSS/JS 在进入路由后按顺序加载；并发请求合并，失败可重试，已成功脚本不重复执行。导航切换与登录会话变更有过期结果检查。两个浏览器共 30 项相关页面测试通过。 |
| C2 | 已修复 | 停止新建与写入 `collaboration_consumptions`、`dot_message_handling_history` 两张无消费者的表，继续使用当前处理状态与规范操作回执；升级不删除旧表或历史数据。 |
| C3 | 已处理 | 保留更新器和源码安装需要的生成物；增加 `.gitattributes` 生成文件标识，降低评审噪音，并持续校验源码/生成物一致。直接删除这些文件会破坏免构建更新路径。 |
| C4 | 已修复 | `/static` 仅服务已验证发布清单内的资源；缺失清单时不回退到整目录开放。包清单、源码模块、构建脚本与 node_modules 等非公开文件返回不可用。 |
| C5 | 已修复 | 移除 macOS 分片中重复的前端构建、Ruff、mypy、npm audit；APT 配置提取为共用脚本。保留不同平台的 pip 安全检查，因为平台条件依赖并不完全一致。 |
| C6 | 已修复 | 必需文件改由 Git 源文件生成 `shared/source_inventory.json`，保留少量启动必需项与公开文档筛选；新增、移动、删除通过生成器与 CI 校验。打包时先检查文件类型/大小，再拒绝清单未登记的源码，不能静默遗漏新增文件。实际完整包及更新包均验证通过。 |
| C7 | 已修复 | Python 统一 100 列格式化并加入持续检查，拆开高密度语句；纯格式化阶段 436 个文件 AST 比对无语义变化，最终 437 个生产/脚本文件格式检查通过。没有为减少单文件行数做缺乏功能边界依据的大规模搬移。 |
| C8 | 已处理 | 删除 5 个干净且已合并的工作树及对应分支，清理 176 个可再生成环境/测试目录（约 329 MiB）、72 个 Finder 元数据文件及过期根目录发布记录。保留脏/未合并工作、6 份 stash、唯一旧发行包与历史证据；名称或日期不足以证明它们可删除。清理清单写入本机验证记录。 |

### 0.4 退役与归档

| 项目 | 处置 | 当前行为与证据 |
| --- | --- | --- |
| D1 | 已清理 | 删除不再引用的旧 `deploy/browser-extension/` 七个文件；实际使用的 web 扩展及生成 ZIP 保留。 |
| D2 | 已清理 | 两个参考适配器移至 `docs/examples/` 并调整测试/文档引用；删除未使用的旧 retention 模块，保留实际生产历史清理实现。 |
| D3 | 已处理 | 四个旧品牌入口保留为兼容垫片，脚本入口直接调用当前实现，不再形成多层别名。不能从仓库内无引用推断用户已经安装的旧启动器全部迁移。 |
| D4 | 已处理 | 15 个专门验收工具纳入 `docs/VERIFICATION_TOOLS.md`，标明用途和平台要求；MCP SDK 检查实际已有测试调用，原静态判断不完整。原生聊天脚本共用辅助实现，保留各自验收场景。 |
| D5 | 已清理/接入 | 删除确实无用的队列、聊天、consumer、前端帮助方法和旧 Runtime 包装；测试转向真实异步实现。运行环境帮助函数接入 Agent，历史规划函数接入维护 CLI；`JoinResult` 作为有效 API 契约保留。 |
| D6 | 已清理/保留 | 删除空 experience.css；Agent 示例配置补入安装文档；旧 SECURITY.md 路径作为外部链接兼容入口保留；根目录历史 BUG_REPORT 移入受控归档。 |
| D7 | 已归档 | 20 篇历史记录移至 `docs/archive/`，重写相对引用并建立完整仓库索引；公开导航只引用公开文件。已有本机私有历史与忽略规则保留，未将其意外纳入发布。 |
| D8 | 已重命名 | 15 个日期命名测试改为所测模块/行为名称，同步脚本、CI 与文档引用，不保留会造成重复收集的测试垫片。 |

### 0.5 验证及边界

- 40 个核心、安全、历史、安装、协作与更新测试模块：**1,496 passed**。
- 9 个发布、安装、生成物与配置测试模块：**132 passed**。
- 官方 MCP SDK 的双协议、工具目录与只读授权互操作：**10 passed**；PR 冒烟测试：**21 passed**。
- Chromium/WebKit 协作中心、VPS、MCP 网关流程：**30 passed**；Node 资源加载器：**2 passed**。
- Python 3.9.25 宿主脚本检查：**13 个通过**；Ruff、437 个文件格式检查、9 个模块 mypy、77 项依赖锁及 22 项部署设置检查均通过。
- 精简后的 Hub 在仅安装 31 个 Hub 锁定依赖的独立环境中启动，健康检查成功，并生成约 669 KB 的 Agent 安装包；未安装 PyYAML、tree-sitter-typescript 或 pywinpty。
- 前端 Prettier 与 Markdown 本地链接检查通过；生成器无漂移；公开资源与源文件清单一致；实际完整源码包和面板更新包通过内容、权限、大小、SHA 和明显凭据模式检查（发现数 0）。更新包分别通过原版 **1.13.0、1.30.5 和当前更新器**解包验证。
- 验证记录位于 `.work/independent-20261111a-*`。一次误用全局 pytest lastfailed 缓存的重跑选中了旧浏览器失败记录，因未设置浏览器缓存目录而失败；随后按本轮明确测试清单及浏览器路径完成上述验证，不把环境失败算作通过。
- 本轮未运行完整 12.5k 测试，也未实际安装 Windows 服务、调用真实微信/原生模型账号或更新生产 VPS。对应平台/渠道工具已保留，不能把契约测试写成生产部署验收。


## 1. 范围、方法与可信度

- **快照**：`main` @ `a89f5f9`（领先 `origin/main` 1 个提交），2026-11-11 09:55–10:15（UTC+8）。
- **工作区在变化**：审查开始时有 88 个未提交文件，结束时为 113 个，说明另一条修复线正在同时修改。下文凡涉及未提交内容的条目都标了「工作区」。行号以读取时为准。
- **规模**：1,405 个受控文件；生产代码约 11.1 万行（hub 5.9 万、agent 2.0 万、shared 1.3 万、scripts 2.0 万），web 5.0 万行，tests 16.3 万行（673 个文件），docs 2.4 万行。
- **逐文件阅读**：根目录全部配置、`compose.yml`、`Dockerfile`、`deploy/` 全部文件、`.github/` 全部工作流、`requirements/`、README、`docs/README.md`、`skills/`，以及 `hub/__main__.py`、`hub/app.py`、`hub/config.py`、`hub/static_assets.py`、`shared/config.py`、`shared/util.py`、`shared/hub_data_contract.py`、`agent/config.py`。
- **工具扫描覆盖全仓**：导入图（模块级引用）、定义/引用计数（函数级死代码）、函数体重复、环境变量三方对照（代码 / Compose / `.env.example` / 文档）、SQL 表与索引读写对照、Markdown 链接与锚点、网页资源引用、依赖锁一致性、扩展规则集 Ruff、全量 mypy（只取高信号类别并逐条复核）。
- **没有做的**：没有逐行读完 11 万行业务代码；没有跑 pytest（全量 12.5k 项，且工作区正在被修改）；没有 fetch 远端；没有做浏览器、Windows/Linux、真实渠道或生产环境验证。静态分析对动态分发（`getattr`、字符串拼接的模块名）不可见，D 节的「无引用」均指**仓库内静态无引用**，删除前仍需按条确认。

严重度：**中** = 用户/运维会实际踩到，或有安全/数据含义；**低** = 维护成本、口径或清洁度问题。

## 2. 最值得先处理的 8 项

| # | 严重度 | 一句话 | 详见 |
| --- | --- | --- | --- |
| 1 | 中 | 审计脱敏 `safe_summary` 在两种参数形状下不按键名脱敏，`password` / `token` / `content` 原文进入审计摘要（已实测） | A1 |
| 2 | 中 | README 快速开始克隆 `v1.33.12`，但本地最新标签是 `v1.33.11`，`RELEASE.json` 为 `published:false` | A2 |
| 3 | 中 | `compose.yml` 的环境变量是白名单，`CODEPIER_OIDC_*` 等 19 个代码会读的变量没有透传，写进 `.env` 不生效 | B1 |
| 4 | 中 | `deploy/mcp-stdio.sh` 等 4 个脚本没有可执行位，但被当成可直接执行的路径使用 | A3 |
| 5 | 中 | CI 只有手动触发；Dependabot 每周开的 PR 没有任何自动检查 | B3 |
| 6 | 中 | 面板首屏一次加载 38 个脚本 + 19 个样式表，包含默认关闭的协作中心（280 KB） | C1 |
| 7 | 中低 | 两张表只写不读：`collaboration_consumptions`、`dot_message_handling_history` | C2 |
| 8 | 低中 | 「工作区」`scripts/panel_update_retention.py:171` 新加的 `zip(strict=True)` 在宿主 Python 3.9 上会抛 `TypeError` | A4 |

---

## A. 损坏或会出错

### A1（中）审计脱敏被特例分支绕过

位置：`shared/util.py::safe_summary`，第 126 行（`upload_id` + `data`）与第 132 行（`action in {fill,key,navigate,select}`）两个提前返回分支。

函数文档写明「不要把文件内容、凭据或命令输出复制进审计输入」。通用分支按键名（`password`、`token`、`content`、`secret`…）替换为 `<N chars>`，但上述两个分支对同级其余键只递归处理**值**，不再看**键名**。实测：

```text
safe_summary({'action':'fill','value':'x','password':'hunter2-plain','content':'file body here'})
→ {'action':'fill','value':'<private input>','password':'hunter2-plain','content':'file body here'}

safe_summary({'upload_id':'u1','data':'AAAA','token':'tok-plain-123','content':'body'})
→ {'upload_id':'u1','data':'<private binary chunk>','token':'tok-plain-123','content':'body'}

safe_summary({'password':'hunter2-plain','content':'body'})          # 通用分支，行为正确
→ {'password':'<13 chars>','content':'<4 chars>'}
```

当前工具参数是否真的会同时带这些键，需要对照各工具入参确认；但脱敏不应依赖调用方恰好不这么传。建议：先做统一的按键名脱敏，再叠加各形状的专用规则，并补回归用例。

### A2（中）README 安装命令指向尚不存在的标签

`README.md:27` 为 `git clone --branch v1.33.12 --depth 1 …`。本地标签最新为 `v1.33.11`（`git tag -l v1.33.12` 为空），`RELEASE.json` 为 `status: prepared / published: false`。在标签推送之前，按 README 操作会直接失败。未 fetch，远端是否已有该标签未核实。建议：README 在发布完成后再切换版本号，或快速开始改用 `releases/latest`。

### A3（中）被直接执行的脚本没有可执行位

Git 记录的模式：`deploy/mcp-stdio.sh`、`deploy/start-agent.sh`、`deploy/install-agent.sh`、`deploy/install-hub.sh` 均为 `100644`；只有 `install.sh`、`codepier`、`deploy/install-from-hub.sh` 是 `100755`。

- `hub/secure_mcp_tunnel.py:163-168` 生成的 tunnel 配置把 `<install_dir>/deploy/mcp-stdio.sh` 直接作为命令（`shlex.join([bridge_path])`），按 README 的 `git clone` 方式安装后执行会得到 `Permission denied`。
- `deploy/install-agent.sh:23` 安装完成后提示「启动：…/deploy/start-agent.sh」，同样不可直接执行。
- `Dockerfile:30` 的 `chmod -R a=rX,u+w` 只对已有执行位的文件保留执行位，不会补上。

建议：`git update-index --chmod=+x` 这四个文件，或让生成的命令显式写成 `bash <path>`。

### A4（低中，工作区）宿主侧脚本引入 Python 3.10+ 语法

`scripts/panel_update_retention.py:171` 在未提交改动中变为 `zip(objects, ids, strict=True)`。该脚本由运维在**宿主机**直接运行（`docs/PANEL_UPDATE.md:162`），而宿主 Python 的声明下限是 3.9（`deploy/install-hub.sh:16,43-44`、`docs/START-HERE.md:16`）；3.9 上会抛 `TypeError: zip() takes no keyword arguments`。

根因是结构性的：`ruff.toml` 的 `target-version` 是 `py313`，新启用的 B905 规则会把 `strict=` 推进所有脚本，而 CI 只跑 3.13，**没有任何门禁保证宿主侧脚本（`migrate_hub*.py`、`rename_checkout.py`、`panel_update*.py`）在 3.9 上可运行**。其余宿主脚本本次检查未发现 3.9 不兼容语法。建议：对宿主侧脚本单独设 `target-version = "py39"` 并排除 B905，或把宿主下限提到 3.10 并同步文档。

### A5（低）条件表达式两支相同

`scripts/check_native_chat_complete_real.py:186-187`：

```python
case['same_native_history'] = (row['native_thread'] == first_native_thread if cli == 'codex'
    else row['native_thread']==first_native_thread)
```

两支完全一样，非 Codex CLI 的「续接同一原生历史」校验没有被区分出来，疑似遗漏。

### A6（低）旧明文历史的加密迁移没有运维入口

`hub/operation_history.py:166 encrypt_legacy_history` 设计为「显式、可续跑、不在启动时调用」的迁移，`docs/OPERATION_HISTORY_PRIVACY.md:17` 只给出函数调用形式。仓库内没有任何 CLI 子命令或脚本调用它（仅测试）；工作区新增的 `scripts/maintain_history.py` 只接入了 `prune_history`。结果是升级前写入的明文 `operations.result/output` 实际上无法由运维按文档完成加密。同文件的 `retention_plan`（第 41 行）也只有测试调用。

### A7（低）关闭顺序里有一处没有保护

`hub/app.py:97-98`：`await runtime.panel_agent_rollouts.stop()` 与 `await oidc.stop()` 写在同一个 `try` 里，前者抛错时后者被跳过；同一个 `lifespan` 里其余停止步骤都各自有 `try/finally`。

---

## B. 不一致

### B1（中）Compose 环境变量白名单与代码读取的变量不一致

`compose.yml` 用显式 `environment:` 列表、没有 `env_file`，因此只有列出的变量会进容器。以下变量 Hub 代码会读，但三个 Compose 文件都没有透传，写进 `.env` 不会生效：

- `CODEPIER_OIDC_*` 共 13 个（`hub/config.py:37-55`、`hub/oidc.py`）。`docs/MULTIUSER_OIDC.md:222` 只说明「容器部署（见 k3s）」可用环境变量声明提供方，没有说明 Compose 不支持。
- `MCP_ALLOWED_ORIGINS`（`hub/mcp.py:61`）。`shared/settings_catalog.py:85` 把它列为部署环境设置项，面板会引导运维去改一个改了也无效的变量。
- `CODEPIER_RELAY_BATCH_ENABLED`、`CODEPIER_RELAY_BATCH_SHADOW`、`CODEPIER_EXPERIENCE_PROJECTION_ENABLED`（`hub/collaboration/config.py:39-40`）。`docs/DOTS_ECOSYSTEM_IMPLEMENTATION_2026-10-12.md:103-104` 把它们写成回滚开关。
- `CODEPIER_MCP_RESPONSE_BYTES`（`shared/core_contracts.py`）、`HUB_HOST`（`hub/__main__.py:69`，且任何文档都没提）。

建议：要么在 `compose.yml` 补透传（或改用 `env_file`），要么在文档里写明这些变量只在 k3s/原生部署下可用。

### B2（低）`.env.example` 与 Compose、代码的出入

- Compose 已透传但 `.env.example` 未提：`CODEPIER_KNOWLEDGE_AUTO_ENROLL`、`CODEPIER_ROOM_HOST_CONNECTIONS_MODE`、`CODEPIER_WEIXIN_APPROVED_API_BASES`、`MCP_BLOCK_NATIVE_COMPUTER`。
- 默认值不一致：`.env.example` 写 `CODEPIER_HUB_CPUS=2.0`，`deploy/compose.performance.yml` 的默认是 `1.5`。
- 代码读取但全仓无文档：`HUB_HOST`、`MCP_ALLOWED_ORIGINS`、`CODEPIER_MCP_PROFILE`、`CODEPIER_RETRY_SECONDS`。
- `.env.example:52` 指向 `docs/COLLABORATION.md`，该文顶部已声明被 `COLLABORATION_CENTER.md` 取代。
- 注释里保留了「v1.1：」和「Oracle 云防火墙」这类特定历史/厂商措辞。

### B3（中）CI 触发方式与周边配置对不上

- `.github/workflows/ci.yml` 只有 `workflow_dispatch`，push 与 PR 都不触发。
- `.github/dependabot.yml` 每周为 pip / npm / github-actions 开 PR，这些 PR 没有任何自动检查。已知远端引用里还留着 `origin/dependabot/pip/certifi-2026.10.23`，而 `certifi==2026.10.23` 已经固定进 `requirements-dev.in`。
- `ci.yml` 的 `complete` 作业用 `github.event.pull_request.base.sha || github.event.before` 作差异覆盖率基线，这两个字段在手动触发下恒为空，实际永远回退到 `HEAD^`，只度量最后一个提交。
- `.github/workflows/image.yml` 有 9 处 `github.event_name != 'pull_request'` 条件，但该工作流没有 PR 触发器，属于死条件。
- `.github/workflows/development-snapshot.yml` 监听 `feat/multiuser-oidc` 的 push；本地与已知远端引用中都没有这个分支。
- `ci.yml` 的 `source` 与 `oidc-authentik` 作业里 action 固定哈希没有版本注释，其余作业都有（`# v7.0.1` 等）。

### B4（低）Python 版本口径有三套

宿主安装脚本 3.9+；手工 Agent 安装 3.12+（`deploy/install-agent.sh:6`、`deploy/install-agent.ps1:11`）；Docker、受管 Agent 安装器、CI、Ruff、mypy 全部是 3.13。3.9 与 3.12 都没有测试覆盖，「3.12 可用」是未验证的声明。

### B5（低）手工 Agent 安装在两个平台上不是同一件事

`deploy/install-agent.sh` 在源码目录内建 `.venv`、`agent init`、前台 `start-agent.sh`；`deploy/install-agent.ps1` 走 `scripts/install_agent.py` 安装到 `~/.codepier-agent` 并注册后台服务。同名入口，安装位置、生命周期和升级路径都不同。

### B6（低）分层方向与目录用途不一致

- `hub/native_file_ingress.py:15` 导入 `agent.incoming_artifacts`（Hub 依赖 Agent 包）。
- `hub/panel_update.py:19` 导入 `scripts.panel_update_source`（运行时依赖 `scripts/`）。
- 后果：`Dockerfile:23-25` 必须把 `agent/`、`scripts/`（83 个脚本，含基准、CI 辅助、UI 审计工具）和整个 `deploy/` 复制进 Hub 运行镜像。
- `requirements/requirements.in`（Hub 运行依赖）包含只有 Agent 用到的包：`PyYAML`（仅 `agent/skills.py` 导入）、`tree-sitter-typescript`（仅 `agent/symbols.py`）、`pywinpty`。

### B7（低）同一段服务归属逻辑有三份逐字相同的副本

`_launchd_service_name`（34 行）、`_read_owned_service_file`（20 行）、`is_legacy_launchd_name`、`systemd_quote` 在 `scripts/agent_lifecycle.py`、`scripts/install_agent.py`、`shared/brand_migration.py` 中各有一份，AST 完全一致。这是判断「服务文件是否归本安装所有」的安全相关逻辑，三处需要同步修改。`install_agent.py` 需要能单文件运行可以理解，另外两处可以共用。

### B8（低）文档口径

- `README.md:136` 与 `:162` 把 `docs/COLLABORATION_CHATROOM.md` 作为「协作中心指南」入口；该文、`COLLABORATION.md`、`DOT_COLLABORATION.md` 顶部都已注明「当前流程见 `COLLABORATION_CENTER.md`」。`DOT_BIDIRECTIONAL_CHAT.md` → `DOT_SEAMLESS_CONVERSATION.md` → `OWNER_TASK_CONTEXT.md` 是另一条层层取代的链。
- `docs/FILE_IMPORT_INGRESS.md:3` 自述「本页描述当前源码（v1.22.0）」，README 仍把它当作当前权威说明。
- `README.md` 的「最近版本变化」有 25 条、一直回溯到 v1.30.2，与 `CHANGELOG.md` 重复；其中缺 v1.33.4 和 v1.33.2，列表中空行有无也不一致。
- `docs/README.md` 结尾在收束段落之后又追加了两条游离条目；同一段写着「仓库根部只保留 README、许可证、安全/贡献入口和固定文件」，但根目录有 96 KB 的 `BUG_REPORT_2026-10-20.md`。
- 文件入站有三篇部分重叠的文档：`FILE_IMPORT.md`、`FILE_IMPORT_INGRESS.md`、`hub-file-import-settings.md`（后者是仓库里少数英文文档之一）。

### B9（低）`skills/` 两个清单已过期（最后修改 9 月 18 日）

- `skills/hub-agent-upgrade/SKILL.md`：仍写「v1.3.0 的 Hub 数据库升级到 schema 4」「tools/list 的 31 个工具」，备份只提 `master.key`（现在还有 `master.keys.json`）。当前默认目录是 `query` / `act` / `get_profile`。
- `skills/review-and-verify/SKILL.md`：使用旧工具名 `project_context`、`workflows_list`、`workflows_update`，写着「此目录可能不是 Git 仓库」，前端检查只列 `web/app.js` 和 `web/workflows.js`，并要求直接跑全量 `pytest -q`。

### B10（低）命名与文案残留

- `deploy/hub.service:3`：`Description=CodePier CodePier Hub`（品牌迁移时重复）。
- `hub/app.py:113`：Hub 的 FastAPI `title="CodePier Agent"`。
- `shared/util.py::atomic_json` 临时文件前缀仍是 `.rd-`（remote-dev 时代）。
- 时区默认值 `Asia/Taipei` 写死在 `shared/config.py:56`、`Dockerfile`、`compose.yml`、`.env.example`、k3s 模板五处。

### B11（低）检查配置里的小出入

- 3 处 `noqa` 针对的是项目并未启用的规则：`scripts/publish_release.py:17-18`（E402）、`scripts/ui_design_audit.py:96`（E731）。
- 「工作区」`pyproject.toml` 的 mypy 清单新加入了 `hub/collaboration/retention.py`，而这个模块没有生产调用方（见 D2）。
- 全仓 68 处未使用导入（`hub/app.py` 10 处、`hub/mcp.py` 7 处）。`ruff.toml` 注明兼容性再导出是有意保留，但没有 `__all__` 或逐行标注区分「有意」和「遗留」。

---

## C. 低效

### C1（中）面板首屏没有按需加载

`web/index.html` 用 38 个 `<script defer>` 和 19 个样式表一次性加载全部功能，包括默认关闭的协作中心（`collaboration-room.js` 5,628 行 / 280 KB，另加 2,739 行样式）、VPS、MCP 网关、Token 用量、调度设置等。带内容哈希的 URL 让二次访问可长缓存，但首访与每次发版后都要完整下载并解析约 57 个资源。建议：至少对按功能开关关闭的模块延迟到进入对应路由时再加载。

### C2（中低）只写不读的表

- `collaboration_consumptions`（建表 `hub/collaboration/schema.py:91`）：唯一的访问是 `hub/collaboration/service.py:697` 的 `INSERT OR IGNORE`。生产代码和测试都没有 `SELECT`；`ack` 无论是否新插入都返回 `consumed: True`。
- `dot_message_handling_history`（`hub/collaboration/message_handling.py:31`）：每次处理修订在第 115 行写入整条规范化记录，生产代码和测试都没有读取，也没有清理路径，只随父消息级联删除。

如果它们是留作审计证据，建议在文档里写明并纳入保留策略；否则可以停止写入。

### C3（低）生成物与源码一起入库

`web/mcp-apps/changes-v1.html`（635 KB，仓库最大的受控文件）、`web/collaboration-room.js`、`web/core/bundle.js`、`web/chat.js`、`web/chat.css`、`web/collaboration-room.css`、`web/index.html`、`web/browser-extension.zip` 都是由相邻源文件构建出来的。任何源文件改动都会带出一份大的生成物 diff；CI 靠 `git diff --exit-code -- web` 保证同步。这是有意的设计（更新器直接用源码包），代价是仓库体积和评审噪音。

### C4（低）`/static` 暴露了整个 `web/` 目录

`hub/app.py:142` 把 `web/` 整体挂载；`hub/static_assets.py:73-76` 对不带哈希的请求直接走目录文件。于是 `web/mcp-apps/package.json`、`package-lock.json`、`build*.mjs`、`web/browser-extension/*.test.mjs`、`index.source.html`、未打包的 `chat/*.js` 与 `collaboration-room/*.mjs` 都可被匿名取到；源码方式部署时如果存在 `web/mcp-apps/node_modules` 也会被服务（镜像构建有 `.dockerignore` 排除）。不是泄密，但清单已经存在，只服务清单内文件更简单。

### C5（低）CI 内的重复工作

`source` 作业与 `verify` 的 macOS shard 0 各跑一遍前端构建、Ruff、mypy、`pip_audit`、`npm audit`；`verify` 和 `oidc-authentik` 两个作业里有一段逐字相同的 APT 源与超时配置（约 20 行）。

### C6（低）手工维护的发布文件清单

`scripts/build_source_bundle.py` 里硬编码了数百条必需文件路径，每加一个文件都要手工登记（例如工作区刚加的 `scripts/maintain_history.py` 已登记在第 146 行，而文件本身还未入库）。

### C7（低）代码密度影响评审效率

生产代码中一行多语句 2,295 处（冒号后接语句 1,303、分号 992），超过 200 字符的行 524 行，超过 400 字符的 52 行；`hub/runtime.py` 1,998 行、`scripts/install_agent.py` 1,759 行、`agent/runner.py` 1,694 行。`.prettierrc.json` 给前端定了 100 列，Python 侧没有对应约束。这不是功能缺陷，但 diff 难读、行级 blame 失效。

### C8（低）本机工作目录的占用（均已被忽略，不影响仓库）

- `.work/` 10 GB、294 个条目（历次审查与验收的临时环境和证据）。
- `dist/` 385 MB、198 个条目，多为 1.10.x 时期的源码包。
- `docs/` 本地 191 MB，其中 `docs/evidence/`、`docs/history/` 和约 50 篇被 `.gitignore` 逐条列出的旧文档只存在于本机。
- 27 个额外 worktree、28 个本地分支（其中 11 个已合并进 `main`）、6 个 stash。
- 根目录的 `LOCAL_RELEASE.json`（记录的是 1.10.2）与 `MANIFEST.sha256`（9 月 14 日）已过期；各目录下散落 `.DS_Store`。

---

## D. 不再需要或可清理（仓库内静态无引用，删除前逐条确认）

### D1 整个 `deploy/browser-extension/` 目录

7 个文件，最后修改于 9 月 20 日的首次发布。代码、测试、文档、CI 中都没有对这个路径的引用；实际使用的扩展源在 `web/browser-extension/`（`scripts/build_integration_assets.py:15`、`agent/install_browser_bridge.py:66`），`web/browser-extension.zip` 也是从后者构建的。两份 `manifest.json` 版本号同为 `1.0.0`，但文件组成、名称、CSP 都不同，并且旧的这份还会随 `deploy/` 被打进 Hub 镜像。

### D2 没有生产调用方的模块

| 模块 | 行数 | 现状 |
| --- | --- | --- |
| `shared/subscription_lifetime.py` | 366 | 只有 `tests/test_subscription_lifetime.py` 导入。`hub/collaboration/events.py:166` 用到的同名元数据来自 `channel_capabilities.subscription_proposal`。 |
| `shared/dot_outbox.py` | 277 | 设计文档称其为「可选参考适配器」，只有测试导入。 |
| `hub/collaboration/retention.py` | 104 | `RetentionPolicy` 无任何引用；`history_retention_plan` 只有一个测试导入。实际清理逻辑在 `history_retention.py`。 |

### D3 旧品牌别名（各 5 行）

`agent/relay_control.py`、`agent/relay_browser_host.py`、`scripts/relay_control.py`、`scripts/relay_browser_host.py`。唯一相关引用是 `shared/brand_browser.py:130` 把旧路径字符串替换成新路径的迁移代码；如果已安装的旧启动器都已迁移完成，这四个文件可以退役。`scripts/` 下的两个是别名的别名。

### D4 没有任何引用的脚本

代码、文档、CI、其他脚本都未提及（11 个）：

`scripts/browser_check.py`、`capture_chat_window.py`、`check_codepier_migration_macos.py`、`check_codepier_proxy_docker.py`、`check_lsp_real.py`、`check_mcp_compat.py`、`check_native_chat_complete_real.py`、`check_native_chat_real.py`、`check_native_windows.py`、`rehearse_host_engine.py`、`ui_unification_capture.py`。

其中 `check_native_chat_complete_real.py`（271 行）与 `check_native_chat_real.py`（247 行）大段重复（`wait`、`snapshot`、`completed`、`stop` 完全相同）。

只被测试引用、没有被 CI 或文档调用：`scripts/check_ci_promotion.py`（581 行，名字像 CI 门禁但两个工作流都没调用）、`check_native_worker_real.py`、`ui_comfort_audit.py`、`check_codepier_migration_docker.py`。

### D5 未使用的函数、方法与类

| 位置 | 名称 | 引用情况 |
| --- | --- | --- |
| `agent/resource_queue.py:218` | `ResourceQueue.live_writers` | 无 |
| `hub/collaboration/chatroom.py:639` | `ChatroomService.conversation_jobs` | 无 |
| `hub/collaboration/consumer_engine.py:29` | `FencedConsumerEngine._all` | 无 |
| `web/agent-install.js:26` | `agentExecutionHelp()` | 无 |
| `hub/runtime.py:245` | `Runtime.detach_connection` | 仅测试 |
| `hub/runtime.py:66` | `remote_codex_denial` | 仅测试 |
| `agent/run_directories.py:73` | `run_environment` | 仅测试 |
| `shared/collaboration_contracts.py:296` | `JoinResult` | 仅测试 |
| `hub/operation_history.py:41` | `retention_plan` | 仅测试（另见 A6） |

### D6 零散文件

- `web/experience.css`：一行「Retired」注释的空占位，入口已不加载，仅有测试断言它**不被**加载。
- `agent/config.example.json`：内容能通过 `validate_config`，但全仓没有任何地方引用它。
- `docs/SECURITY.md`：3 行兼容桩，指回根目录 `SECURITY.md`（`scripts/build_source_bundle.py:296` 仍把它列为必需文件）。
- `BUG_REPORT_2026-10-20.md`（根目录，96 KB）：历史扫描报告，自述「总代码行数约 80.9 万行」，处置记录在 `docs/BUG_FIX_VERIFICATION_2026-10-20.md`。

### D7 一次性文档

127 篇受控文档中，约 60 篇是带日期的设计/验收/实施/审查记录。35 篇没有任何 Markdown 入链，其中下列 17 篇在全仓零提及：

`ASTRA_ESTIMATION_CALIBRATION_2026-10-25`、`BUG_FIX_VERIFICATION_2026-10-20`、`INDEPENDENT_DEVICE_USE_DESIGN_2026-10-21`、`MCP_CONTEXT_OPTIMIZATION_2026-11-10`、`MCP_PROJECT_RUNTIME_IMPLEMENTATION_2026-10-16`、`MCP_PROJECT_VIEW_AND_AUTHORIZATION_REVIEW_2026-10-15`、`PROJECT_AUDIT_2026-11-11`、`QUEUE_PANEL_IMPLEMENTATION_RESULTS_2026-11-05`、`QUEUE_UI_INTEGRATION_RESULTS_2026-11-05`、`ROOM_PROJECT_MANAGEMENT_ACCEPTANCE_2026-10-13`、`SITE_EXPERIENCE_UNIFICATION_2026-10-12`、`SSH_FAILURE_RECEIPTS_20261020`、`UI_DESIGN_SYSTEM_IMPLEMENTATION_RESULTS_2026-11-05`、`UI_LAYOUT_DEEP_AUDIT_2026-11-10`、`UI_UNIFICATION_20261006`、`WINDOWS_INSTALL_RECOVERY_AUDIT_20261011`、`WORKSPACE_PRIVACY_20261006`。

另有自述为历史记录的 `PR3_REVIEW_FIXES.md`、`PR3_SECOND_REVIEW_FIXES.md`、`UPSTREAM_INTEGRATION.md`。`.gitignore` 已有 `/docs/history/` 这个去处；把一次性记录移过去（或建一个受控的 `docs/archive/`）可以让 `docs/README.md` 重新成为完整索引，同时 `.gitignore` 里逐条列举的 60 多行旧文档名也可以收敛。

### D8 按审查日期命名的测试文件（15 个）

`tests/test_bug_report_*_20261020.py`（9 个）、`test_code_audit_*_20261104.py`（3 个）、`test_queue_parallel_20261106.py`、`test_review_20260930_core.py`、`test_scan_hub_20261105.py`。用例本身有效，但按「哪次审查发现的」而不是「测的是哪个模块」来组织，日后找覆盖范围不方便。

---

## E. 核对后没有发现问题的方面

- 版本号一致：`shared/util.py`、`RELEASE.json`、`compose.yml`、k3s 模板、README 均为 1.33.12。
- 6 套依赖锁与各自 `.in` 的直接固定版本一致，跨锁文件无版本漂移。
- 项目默认 Ruff 门禁通过（`agent hub shared scripts tests`），`git diff --check` 通过。
- 扩展规则集下无未定义名、无重复定义、无可变默认参数、无 `shell=True`、无 `verify=False`、无 MD5/SHA1、无未保存的 `create_task`。
- `agent/skills.py:88` 的 `yaml.load` 使用的是 `SafeLoader` 子类；`hub/collaboration/weixin_protocol.py:380,388` 的 ECB 模式属于对接协议要求，未作判断。
- `hub/gateway/validate_worker.py` 实测：合法值返回 0，不合法值和远程 `$ref` 返回 1。
- 全量 mypy 的 `name-defined`、`call-arg` 告警逐条复核均为误报（闭包参数、`*denial` 展开、未识别的 attrs 类）。
- Markdown 本地文件链接无缺失；`#p1…#p32`、`#section-N` 这类锚点都有对应的 `<a id>`。
- 测试无同文件重名用例、无无条件 `skip`/`xfail` 标记；`tests/` 下的支持模块全部有引用。
- 按文件名核对，没有受控的凭据、数据库、日志、`__pycache__` 或 `.DS_Store`（未做内容级密钥扫描，项目自带的 `check_release.py` 负责这一项）。
- 255 张表中除 C2 两张外都有读路径；182 个索引未发现前缀冗余。

## F. 建议的处理顺序

1. **A1、A3、A2**：改动小、影响明确。A1 需要补回归用例。
2. **B1**：先决定这些变量在 Compose 下要不要支持，再改 `compose.yml` 或改文档。
3. **A4**：趁 B905 改动还没提交，先定宿主脚本的 Python 下限和对应的 lint 配置。
4. **B3**：决定 CI 是否对 PR 至少跑 `release-source` 模式，否则关掉 Dependabot 的自动 PR。
5. **D1–D5**：按条确认后删除，建议一条一个提交，方便回退。
6. **C1、C2**：需要先有数据（首屏耗时、两张表的生产行数）再决定投入。
7. **B8、B9、D7**：文档与 `skills/` 的整理可以合并成一次纯文档提交。
