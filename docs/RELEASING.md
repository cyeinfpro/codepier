# GitHub 发布清单

## 发布前

源码版本以 `shared/util.py` 为准；同步 `RELEASE.json`、Compose 与 k3s 清单的镜像标签及发布说明。为发布验证建立独立 Python 环境，运行 Python 依赖审计、npm 审计、Ruff、MCP Apps 构建和安装脚本语法检查。不要升级现用服务来代替源码验收。

单独执行完整回归时，使用新的证据目录。确认报告中的所有收集用例都执行通过，没有意外跳过、失败、超时、强制终止或源码变化。真实 Windows/macOS/Linux 服务安装卸载、Docker 数据迁移及外部 MCP 客户端需要单独的隔离演练；仅有模拟测试时必须明确标注。

## 创建公开源码包

```bash
VERSION=$(.venv/bin/python -c 'from scripts.build_source_bundle import source_version; print(source_version())')
.venv/bin/python scripts/check_release.py
.venv/bin/python scripts/build_source_bundle.py --public
.venv/bin/python scripts/check_release.py --bundle "dist/codepier-${VERSION}-source-full.zip"
.venv/bin/python scripts/build_source_bundle.py --public --panel-update
.venv/bin/python scripts/check_release.py --panel-update --bundle "dist/codepier-${VERSION}-source.zip"
```

正式 Release 同时上传两种 ZIP 及各自 `.manifest.json`。`source-full.zip` 是完整开发源码，包含格式、测试配置与依赖输入；`source.zip` 是旧面板更新器识别的固定附件名，仅排除 2 个开发专用顶层文件与 requirements/ 下的 6 个依赖输入文件，所有运行代码与锁文件完全一致。排除项在 `PANEL_UPDATE_EXCLUDES` 中显式列出，不放宽更新器安全白名单。兼容检查必须用保留的 1.13.0 原始更新器及当前更新器实际解包更新 ZIP；仅验证新源码或最小测试包不能证明旧面板可升级。

重复构建并比较两种 ZIP 的 SHA-256，确认同一源码输入产生相同归档。归档中的 `MANIFEST.sha256` 必须覆盖全部源文件；外部 `.manifest.json` 提供逐文件清单。检查公开包内没有运行状态、个人服务地址、历史部署证据或凭据。模式扫描不能替代对新增文件和截图的人工检查。

归档校验只接受普通文件、Stored/Deflate 压缩及支持的标记；归档上限为 256 MiB，成员与整体摘要均分块读取。文件在校验期间被替换、截断或修改时，检查必须失败，不能复用之前的成功结论。

## 独立源码验收目录

对最终公开 `source-full.zip` 解包后的源码副本建立隔离依赖环境并执行完整回归；不要仅在包含私有配置、历史生成文件和预装依赖的旧工作目录中验收。解包时保留 ZIP 中记录的可执行权限，并将验收前后的公开源码清单与原 ZIP 逐项比对。生成的测试日志与截图不加入公开源码。

GitHub CI 先构建一次规范公开 ZIP 并重复构建比对哈希，随后每个 Linux/macOS 分片下载同一不可变 public-source artifact，校验并解包运行，不从 checkout 执行业务测试。每片记录精确 commit、ZIP 摘要、内部 manifest、逐文件内容/权限及验收后复核结果；平台聚合同时核对四个普通分片、一个独立资源分片与一个独立长时进程分片恰好一次的完整回归和同一归档证明。同一 job 按对应锁文件新建的依赖环境可链接到解包副本，兼容 SDK 解释器和浏览器缓存使用明确的绝对路径。正式 Release 使用源码构建作业确认的同一份 ZIP。浏览器环境阻塞时，应如实记录该项回归结果。

macOS 下，浏览器原生宿主若从 Downloads 等受限制目录启动，可能出现 `Operation not permitted`，而扩展只显示未连接。保留原始错误，在系统临时目录中的公开源码副本运行隔离浏览器测试；不要扩大浏览器权限、授予整盘访问、跳过用例或增加重试来掩盖失败。测试结束后记录副本路径、依赖版本、完整退出码与源码指纹。

## 初始化与发布仓库

使用公开包解压后的干净目录建立仓库，不要在混有私人资料的旧工作目录直接执行 `git add .`。检查暂存差异和文件列表，确认许可证、第三方声明、README 相对链接和执行权限完整。维护者确认仓库所有者、名称、可见性和 Git 提交身份后，才创建远程仓库并推送。

建议分支名 `main`。在 GitHub 启用私密漏洞报告、依赖安全告警。分支保护可按需配置，完整 CI 由维护者手动触发；禁止把生产凭据暴露给 PR 工作流。CI 工作流只检查和构建；容器镜像工作流把通过启动验证的 Hub 镜像发布到 GHCR，但同样不会创建 Release 或部署服务。

正式发布核对同一提交的两个 GitHub 作业。源码作业 **Build verified public source** 校验两种 ZIP、容器运行资格与更新器启动。镜像作业 **Build, boot-test and publish** 验证镜像启动、多架构清单并将该提交的镜像推送到 GHCR。完整 CI、长时测试与跨平台汇总独立报告，发布可以在这些作业仍在运行或失败时继续。

日常推送 main 不触发完整 CI 或镜像构建。准备发版时，先推送最终提交，然后手动触发对应的源码与镜像作业。源码运行号也可来自同一提交上已经完成源码作业的 full 模式。

```bash
gh workflow run ci.yml --ref main -f mode=release-source
gh workflow run image.yml --ref main
```

两项作业通过后，在干净的本地 main 上用对应运行号发布。

```bash
python3 scripts/publish_release.py --repo OWNER/REPO \
  --source-run SOURCE_RUN_ID --image-run IMAGE_RUN_ID --notes-file RELEASE_NOTES.md
```

加 `--check-only` 可预检作业和四个附件，不需要发布说明。发布入口将本地与远端 main、源码工作流运行与尝试编号、源码清单及附件摘要绑定到同一个提交。确认后推送与 VERSION 一致的标签，再上传两种 ZIP 和对应清单。

## 容器镜像

`.github/workflows/image.yml` 用仓库根目录的 `Dockerfile` 构建 Hub 镜像并发布到 `ghcr.io/<仓库所有者>/codepier`；Kubernetes/k3s 等部署直接拉取该镜像，不再依赖 `install.sh` 在服务器上现场构建。在 main 或与源码版本匹配的发布候选分支手动运行，或推送 `v*.*.*` 正式标签时触发。

发布前的门禁在同一任务内完成：宿主机 Python 运行 `scripts/check_release.py` 核对源码版本、RELEASE.json、Compose 与文档一致；标签必须等于 `v${VERSION}`；先构建 linux/amd64 候选镜像，用与 `compose.yml` 相同的只读根文件系统、tmpfs `/tmp`、`cap_drop ALL` 和 `no-new-privileges` 从空数据目录启动，要求 `/healthz` 在 60 秒内返回 `ok` 且版本与源码一致、未处于维护状态、`/agent/manifest.json` 可用、进程 UID 为 10001。任一检查失败即不发布。

通过后构建 linux/amd64 与 linux/arm64 清单并推送，附带 SBOM 与构建来源证明；依赖锁文件由 `uv pip compile --universal` 生成，两种架构都使用同一组哈希校验的 wheel。标签：正式版本 `VERSION`、`MAJOR.MINOR` 与 `latest`；`main` 及手动运行的分支使用分支名；所有构建另加 `sha-<完整 40 位提交>`。任务摘要与 `image-<run id>-<attempt>` 工件记录 `镜像@sha256 摘要`，部署应固定该摘要而不是可变标签。

正式标签由发布入口在同提交的源码和镜像作业通过后创建。首次推送生成的 GHCR 包默认私有：需要在包设置中公开，或为集群配置 imagePullSecret。Compose 安装脚本仍走本地构建路径，不受影响。k3s 单文件部署模板见 `deploy/k3s/deployment.yaml`（单副本 Recreate、local-path 数据卷、非 root 只读容器、Traefik Ingress、不启用宿主机更新服务）；其版本与模板占位符由 `scripts/check_release.py` 核对，部署须使用下文生成的不可变摘要清单。

## 发布后

在测试设备演练升级与回滚/恢复流程，再分批更新实际 Hub 和 Agent。分别核对运行版本、目录、服务名、数据完整性和权限。更新文档中的已知限制，不把未经验证的平台标记为支持验收完成。

## 独立宿主连接灰度

独立宿主接入默认关闭。先部署支持宿主撤销闸的桥接版本（至少 `1.33.9`），停止使用目标数据库的 Hub 实例，并核对部署入口能拒绝低于数据库声明的版本。以下命令在桥接版的运行环境中执行，使用与 Hub 相同的状态目录配置。

```bash
python -m hub room-host-backfill
python -m hub room-host-activate --confirm-compatible-deployment
python -m hub room-host-backfill --apply
```

首条命令只读。检查报告中的身份和往返证据缺口，再激活版本门。重复执行 `--apply`，直到持久进度显示扫描完成。以 `CODEPIER_ROOM_HOST_CONNECTIONS_MODE=shadow` 核对旧来源链与独立宿主判定，再按验收结果切换到 `on`。切回 `off` 可暂停新接入，已有撤销继续生效。回退使用满足数据库版本门的桥接版本。

## 构建与证据门禁

先按 [开发与验证](DEVELOPMENT.md) 安装哈希锁依赖，运行前端格式、类型检查、前端构建和资源清单生成。六片通过只能作为分片证据；每个平台必须合并完整收集清单和覆盖数据，所有测试恰好一次、零跳过、源码不变才算完整。diff coverage 是新增/修改可执行行的观察报告，不强加历史全仓 80% 门槛。

公开源码仅选择审阅后的当前指南；相对链接、README 当前版本、SECURITY 支持分支、生成资源内容哈希和 Compose 版本必须一致。LOCAL_RELEASE.json 留作历史本地记录，不参与当前版本判定，也不证明现用服务已更新。生产密钥轮换需要独立离线维护，不由发布任务触发。

资源分片在独立新 runner 上只执行显式登记的真实 OS/Hub/Agent 准入验收，不在浏览器或全量回归之后运行。它仍从相同公开 ZIP 收集完整测试目录，记录 coverage 和源码前后指纹；平台聚合要求六片完整、资源用例只在专片出现一次。漏标、漏收集、多出的资源用例或混用分片布局均失败。资源不足仍失败，不能伪造健康、降低生产阈值或将跳过当通过。CPU 诊断只输出有界进程名称和百分比，不收集参数、路径或秘密。

macOS 资源专片将本作业生成的虚拟环境、pip 缓存、公开源码解包、临时文件及结果 scratch 放在独立 `.noindex` 目录，工作区只保留依赖和 dist 的符号链接，避免验收生成的大量短期文件触发 Spotlight 导入。只影响该临时 job 的目录布局，不调用 mdutil、不停系统索引、不改变生产压力采样、阈值或准备期限；仍由真实 OS 与 Agent 心跳决定准入。

资源专片在真实业务测试开始前，使用只读 OS 计数最多等待 300 秒，要求连续 15 秒满足原调度器保守增长条件。此阶段不向 Agent 注入健康值、不修改生产阈值、不关闭系统索引，也不重试失败测试；超时会使完整回归失败。日志保存实际 CPU、可用内存、I/O 及等待原因，避免把新 runner 初始化负载误判为产品突发准入能力。

外部 OS 预检不证明 Hub 已扩容。真实 Hub/Agent 进程启动后，128 请求准入验收另有单次准备阶段，以开始后 300 秒为接受准备成功的绝对截止时间，每 3 秒通过同一 HTTP 连接观察真实 Hub 状态；只有经过认证的 Agent 心跳使 Hub 达到原有 adaptive 准入容量才开始突发请求。最后一次在途读取仍可按既有 35 秒网络超时上限返回，因此阶段返回可能晚于截止时间；到达或超过 300 秒的成功不算通过，持续压力、未知状态或请求错误仍超时失败，并保留最后真实状态与错误类型。此等待不重启进程、不自动重跑测试，也不改变生产阈值、128 请求断言或其执行期限；失败仍使完整回归失败。

## 手动回归与发布证据

CodePier CI 的 full 模式运行完整回归。release-source 仅运行源码作业，保留相同的四个发布附件、两份发布检查、source-build.json、runtime-acceptance.json 和 hub-image-acceptance.json。运行资格和 Hub 启动证明继续绑定提交、归档与源码清单。

长时进程模块专片仍实际并行等待 600 与 1800 秒。将它从普通片移出只消除前后排队和其他串行验收拖尾，不能把真实 30 分钟变成几分钟。动态测试集合全部纳入六片恰好一次证明，新增用例只能增加覆盖，不能因为分片而遗漏。这组证据用于完整回归的结论。


镜像复用将完整源码 SHA、Git tree、版本、镜像 workflow 内容哈希及固定构建配方绑定到一次真实成功构建。先用仅本次 run/attempt 的候选标签存储双架构结果，通过真实 amd64 hardening 启动验证，并核对各架构及其 SBOM/来源证明后，才晋级普通分支、正式版本及 latest 标签。同 SHA 的 main/tag 后续运行只读验证成功来源工件和镜像摘要，随后为同一现有 OCI index 摘要添加标签，不重建镜像或改写其来源证明。若候选的 amd64 config 与第一次启动的本地镜像不一致，必须额外启动将被晋级的精确 amd64 镜像并完成同样验证，不能略过差异。

正式 VERSION 与完整 SHA 标签一旦指向另一个摘要就失败，不自动覆盖；main 与 MAJOR.MINOR/latest 仍是明确的可变别名。完整 SHA 级工作流串行化配合仓库镜像发布 job 锁降低本工作流的 main/tag 竞态，不声称约束外部发布者或提供注册表级不可变策略。GitHub concurrency 可能替换尚未开始的旧 pending 运行，因此不承诺每条旧 main 提交都产生镜像。image-proof-SHA-run-attempt 成功证明保留 14 天；首个新流水线版本不能复用旧格式镜像记录，缺失、过期或无法验证时回退完整构建及启动门禁。此流程不创建 Release、不部署，也不设置仓库权限或凭据。


跨运行的 Actions 产物由镜像晋级验证 job 只读获取，使用 contents: read、actions: read 及 GitHub 内置短时令牌。CI 仅需 contents: read。镜像构建 job 在使用证明前重新核对源码身份与注册表摘要。证明缺失、过期或无法核验时重新构建并执行启动门禁。

镜像可复用来源包括同仓库 main 或匹配源码版本的 release/v<VERSION>-<小写字母数字短横线后缀> 分支手动运行。必须使用空输入，来源事件和分支须与证明匹配，且同一仓库、SHA、Git tree、workflow、构建配方、运行编号和产物摘要均通过校验。

### 不可变部署清单与安装恢复

仓库的 deploy/k3s/deployment.yaml 是不可直接部署的模板，RELEASE_DIGEST 必须由镜像工作流在已验证镜像发布后替换。image artifact 内同时保留 deployment.yaml 和 deployment.json，后者绑定源码提交、版本、OCI index digest 与清单 SHA-256；部署前核对四者，不能直接应用可变标签模板。离线生成同一清单可用 python scripts/render_k3s.py --image ghcr.io/OWNER/codepier --digest sha256:VERIFIED_DIGEST --commit EXACT_SOURCE_SHA --output deployment.yaml --metadata deployment.json。不要为填入自身镜像 digest 再修改该版本源码。

Caddy HTTPS overlay 要求 Compose 2.24.4+。它同时设置面板和 MCP 的 HTTPS 公共地址，HTTP 仅绑定宿主回环地址；既有 Agent 应改用 HTTPS Hub 地址后再启用该 overlay。MCP_DOMAIN 与 TLS_EMAIL 默认留空，必须由操作者填写真实配置。Caddy、OIDC 验收 PostgreSQL 与 Authentik 镜像使用经过 registry 元数据核验的不可变多架构 digest，升级时需重新核验后显式修改。

更新器先完整写入 owner 收据，再原子发布安装锁，避免崩溃遗留空锁。历史空锁不可自动删除：先停止更新器和所有安装进程，再运行 python scripts/panel_updater.py recover-install-lock --root /实际安装目录 --confirm-installers-stopped。命令只隔离空的无主锁目录并保留原 inode；有 pid、owner 收据、未知文件或符号链接时拒绝，不接触数据卷。
