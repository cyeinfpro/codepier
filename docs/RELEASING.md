# GitHub 发布清单

## 发布前

源码版本以 `shared/util.py` 为准；同步 `RELEASE.json`、Compose 与 k3s 清单的镜像标签及发布说明。为发布验证建立独立 Python 环境，运行 Python 依赖审计、npm 审计、Ruff、全部测试模块、MCP Apps 构建和安装脚本语法检查。不要升级现用服务来代替源码验收。

完整回归使用新的证据目录。确认报告中的所有收集用例都执行通过，没有意外跳过、失败、超时、强制终止或源码变化。真实 Windows/macOS/Linux 服务安装卸载、Docker 数据迁移及外部 MCP 客户端需要单独的隔离演练；仅有模拟测试时必须明确标注。

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

GitHub CI 先构建一次规范公开 ZIP 并重复构建比对哈希，随后每个 Linux/macOS 分片下载同一不可变 public-source artifact，校验并解包运行，不从 checkout 执行业务测试。每片记录精确 commit、ZIP 摘要、内部 manifest、逐文件内容/权限及验收后复核结果；平台聚合同时核对四个普通分片、一个独立资源分片与一个独立长时进程分片恰好一次的完整回归和同一归档证明。同一 job 按对应锁文件新建的依赖环境可链接到解包副本，兼容 SDK 解释器和浏览器缓存使用明确的绝对路径。正式 Release 的 ZIP 必须与通过这组门禁的归档一致；本机浏览器环境阻塞不能算通过，也不能用跳过、重试或修改权限绕过。

macOS 下，浏览器原生宿主若从 Downloads 等受限制目录启动，可能出现 `Operation not permitted`，而扩展只显示未连接。保留原始错误，在系统临时目录中的公开源码副本运行隔离浏览器测试；不要扩大浏览器权限、授予整盘访问、跳过用例或增加重试来掩盖失败。测试结束后记录副本路径、依赖版本、完整退出码与源码指纹。

## 初始化与发布仓库

使用公开包解压后的干净目录建立仓库，不要在混有私人资料的旧工作目录直接执行 `git add .`。检查暂存差异和文件列表，确认许可证、第三方声明、README 相对链接和执行权限完整。维护者确认仓库所有者、名称、可见性和 Git 提交身份后，才创建远程仓库并推送。

建议分支名 `main`。在 GitHub 启用私密漏洞报告、依赖安全告警与分支保护，并把 CI 设为合并条件；禁止把生产凭据暴露给 PR 工作流。CI 工作流只检查和构建；容器镜像工作流把通过启动验证的 Hub 镜像发布到 GHCR，但同样不会创建 Release 或部署服务。

首次推送后必须核对 GitHub 中真实运行的 CI 结果；本机通过不能冒充远端 CI 通过。随后由维护者确认与 VERSION 对应的 `v${VERSION}` 标签、发布说明和经过校验的源代码包，再执行公开发布。准备完成与已经发布是两个不同状态。

## 容器镜像

`.github/workflows/image.yml` 用仓库根目录的 `Dockerfile` 构建 Hub 镜像并发布到 `ghcr.io/<仓库所有者>/codepier`；Kubernetes/k3s 等部署直接拉取该镜像，不再依赖 `install.sh` 在服务器上现场构建。触发条件：推送 `main`、推送 `v*.*.*` 标签、Pull Request（只构建不发布）以及手动运行（发布当前分支名标签）。

发布前的门禁在同一任务内完成：宿主机 Python 运行 `scripts/check_release.py` 核对源码版本、RELEASE.json、Compose 与文档一致；标签必须等于 `v${VERSION}`；先构建 linux/amd64 候选镜像，用与 `compose.yml` 相同的只读根文件系统、tmpfs `/tmp`、`cap_drop ALL` 和 `no-new-privileges` 从空数据目录启动，要求 `/healthz` 在 60 秒内返回 `ok` 且版本与源码一致、未处于维护状态、`/agent/manifest.json` 可用、进程 UID 为 10001。任一检查失败即不发布。

通过后构建 linux/amd64 与 linux/arm64 清单并推送，附带 SBOM 与构建来源证明；依赖锁文件由 `uv pip compile --universal` 生成，两种架构都使用同一组哈希校验的 wheel。标签：正式版本 `VERSION`、`MAJOR.MINOR` 与 `latest`；`main` 及手动运行的分支使用分支名；所有构建另加 `sha-<完整 40 位提交>`。任务摘要与 `image-<run id>-<attempt>` 工件记录 `镜像@sha256 摘要`，部署应固定该摘要而不是可变标签。

镜像工作流不运行完整回归；正式标签只能在同一提交的 CI 全绿后创建，这与上文发布顺序一致。首次推送生成的 GHCR 包默认私有：需要在包设置中公开，或为集群配置 imagePullSecret。Compose 安装脚本仍走本地构建路径，不受影响。k3s 单文件部署清单见 `deploy/k3s/deployment.yaml`（单副本 Recreate、local-path 数据卷、非 root 只读容器、Traefik Ingress、不启用宿主机更新服务）；其镜像标签由 `scripts/check_release.py` 与源码版本一同核对。

## 发布后

在测试设备演练升级与回滚/恢复流程，再分批更新实际 Hub 和 Agent。分别核对运行版本、目录、服务名、数据完整性和权限。更新文档中的已知限制，不把未经验证的平台标记为支持验收完成。

## 构建与证据门禁

先按 [开发与验证](DEVELOPMENT.md) 安装哈希锁依赖，运行前端格式、类型检查、前端构建和资源清单生成。六片通过只能作为分片证据；每个平台必须合并完整收集清单和覆盖数据，所有测试恰好一次、零跳过、源码不变才算完整。diff coverage 是新增/修改可执行行的观察报告，不强加历史全仓 80% 门槛。

公开源码仅选择审阅后的当前指南；相对链接、README 当前版本、SECURITY 支持分支、生成资源内容哈希和 Compose 版本必须一致。LOCAL_RELEASE.json 留作历史本地记录，不参与当前版本判定，也不证明现用服务已更新。生产密钥轮换需要独立离线维护，不由发布任务触发。

资源分片在独立新 runner 上只执行显式登记的真实 OS/Hub/Agent 准入验收，不在浏览器或全量回归之后运行。它仍从相同公开 ZIP 收集完整测试目录，记录 coverage 和源码前后指纹；平台聚合要求六片完整、资源用例只在专片出现一次。漏标、漏收集、多出的资源用例或混用分片布局均失败。资源不足仍失败，不能伪造健康、降低生产阈值或将跳过当通过。CPU 诊断只输出有界进程名称和百分比，不收集参数、路径或秘密。

资源专片在真实业务测试开始前，使用只读 OS 计数最多等待 300 秒，要求连续 15 秒满足原调度器保守增长条件。此阶段不向 Agent 注入健康值、不修改生产阈值、不关闭系统索引，也不重试失败测试；超时仍使发布门禁失败。日志保存实际 CPU、可用内存、I/O 及等待原因，避免把新 runner 初始化负载误判为产品突发准入能力。

外部 OS 预检不证明 Hub 已扩容。真实 Hub/Agent 进程启动后，128 请求准入验收另有单次准备阶段，以开始后 300 秒为接受准备成功的绝对截止时间，每 3 秒通过同一 HTTP 连接观察真实 Hub 状态；只有经过认证的 Agent 心跳使 Hub 达到原有 adaptive 准入容量才开始突发请求。最后一次在途读取仍可按既有 35 秒网络超时上限返回，因此阶段返回可能晚于截止时间；到达或超过 300 秒的成功不算通过，持续压力、未知状态或请求错误仍超时失败，并保留最后真实状态与错误类型。此等待不重启进程、不自动重跑测试，也不改变生产阈值、128 请求断言或其执行期限；失败仍阻止发布。

## 同提交晋级与长测并行

候选分支在最终源码提交上运行完整 CodePier CI。晋级不是重新生成测试成功状态：scripts/check_ci_promotion.py 只读核验同一仓库、完整 SHA、Git tree、workflow ID/路径/内容哈希、最新有效成功 job、Windows/OIDC、两平台六片完整证明、coverage 及公开归档。public-source 工件包含四个发布附件、两份发布检查、source-build.json 和 runtime-acceptance.json；真实容器资格验证必须从同一公开归档执行，`shared/runtime_qualification.py` 中 `CHECKS` 规定的全部边界、读写、恢复及归档一致性检查通过且清理完成，证明同时绑定提交、归档与源码清单，缺少或不匹配则禁止复用；每个来源工件绑定不可变 artifact ID 与 GitHub 给出的 ZIP digest。过期、缺失、来源不符、后续同 workflow 失败或未结束都使复用失败并回退完整 CI。

可在确认已授权的提交推送及发布范围内，将已通过候选的同一 SHA 快进到 main，核对远端 SHA 后按同一已验证公开 ZIP 创建正式标签及 Release。无需把 main 自动触发的相同完整测试重复等待一遍；main 的快速路径明确保留候选 run、job、artifact、源码和四附件摘要。SHA 或 workflow 有任何变化，之前证据一律不能晋级。镜像门禁仍独立执行，源码证据不能替代镜像启动、多架构、SBOM/来源证明验证。

长时进程模块专片仍实际并行等待 600 与 1800 秒。将它从普通片移出只消除前后排队和其他串行验收拖尾，不能把真实 30 分钟变成几分钟。动态测试集合全部纳入六片恰好一次证明，新增用例只能增加覆盖，不能因为分片而遗漏。取消长测、按改动范围跨 SHA 复用长测或改为定时检查属于另行批准的质量策略，本优化不采用。


镜像复用将完整源码 SHA、Git tree、版本、镜像 workflow 内容哈希及固定构建配方绑定到一次真实成功构建。先用仅本次 run/attempt 的候选标签存储双架构结果，通过真实 amd64 hardening 启动验证，并核对各架构及其 SBOM/来源证明后，才晋级普通分支、正式版本及 latest 标签。同 SHA 的 main/tag 后续运行只读验证成功来源工件和镜像摘要，随后为同一现有 OCI index 摘要添加标签，不重建镜像或改写其来源证明。若候选的 amd64 config 与第一次启动的本地镜像不一致，必须额外启动将被晋级的精确 amd64 镜像并完成同样验证，不能略过差异。

正式 VERSION 与完整 SHA 标签一旦指向另一个摘要就失败，不自动覆盖；main 与 MAJOR.MINOR/latest 仍是明确的可变别名。完整 SHA 级工作流串行化配合仓库镜像发布 job 锁降低本工作流的 main/tag 竞态，不声称约束外部发布者或提供注册表级不可变策略。GitHub concurrency 可能替换尚未开始的旧 pending 运行，因此不承诺每条旧 main 提交都产生镜像。image-proof-SHA-run-attempt 成功证明保留 14 天；首个新流水线版本不能复用旧格式镜像记录，缺失、过期或无法验证时回退完整构建及启动门禁。此流程不创建 Release、不部署，也不设置仓库权限或凭据。


跨运行的 Actions 产物读取仅由 CI 与镜像各自独立的晋级验证 job 执行，job 权限限定为 contents: read 与 actions: read，使用 GitHub 内置短时令牌；不新增凭据或持久授权。CI 的令牌只传给 main push 的验证步骤。普通测试/源码构建 job 不获得 Actions 读取权限；镜像构建与发布 job 保持原有 contents: read、packages: write，不获得 actions: read。镜像只读验证结果通过本次运行的有界 job output 传递，构建 job 在使用前重新核对源码身份与注册表摘要。读取被拒绝、缺少或无法校验的证明一律不允许复用，回到完整构建和既有门禁；取消运行仍尊重取消，不继续发布。


候选镜像可由同仓库的 workflow_dispatch 产生，但可复用来源严格限于与源码版本匹配的 release/v<VERSION>-<小写字母数字短横线后缀> 分支，且 dispatch 输入必须为空、forced 明确为 false。来源 run 的 event/head_branch 必须与 proof.origin.event/ref 一致，并核对同仓库 ID、相同源码 SHA/tree、workflow SHA/内容、固定构建配方、成功的完整构建/真实启动/多架构证明步骤、run/attempt 和 artifact ZIP 摘要；读取后再确认来源 run 未变化。PR、fork、任意 topic/main 手动触发、异版本候选、非空参数或不完整产物都不能作为复用来源。该规则保留正式版本与完整 SHA 标签的不可覆盖检查；改变源码后必须用新 SHA 重跑完整验收。
