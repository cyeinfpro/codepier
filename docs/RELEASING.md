# GitHub 发布清单

## 发布前

源码版本以 `shared/util.py` 为准；同步 `RELEASE.json`、Compose 镜像标签与发布说明。为发布验证建立独立 Python 环境，运行 Python 依赖审计、npm 审计、Ruff、全部测试模块、MCP Apps 构建和安装脚本语法检查。不要升级现用服务来代替源码验收。

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

正式 Release 同时上传两种 ZIP 及各自 `.manifest.json`。`source-full.zip` 是完整开发源码，包含格式、测试配置与依赖输入；`source.zip` 是旧面板更新器识别的固定附件名，仅排除 8 个开发专用顶层文件，所有运行代码与锁文件完全一致。排除项在 `PANEL_UPDATE_EXCLUDES` 中显式列出，不放宽更新器安全白名单。兼容检查必须用保留的 1.13.0 原始更新器及当前更新器实际解包更新 ZIP；仅验证新源码或最小测试包不能证明旧面板可升级。

重复构建并比较两种 ZIP 的 SHA-256，确认同一源码输入产生相同归档。归档中的 `MANIFEST.sha256` 必须覆盖全部源文件；外部 `.manifest.json` 提供逐文件清单。检查公开包内没有运行状态、个人服务地址、历史部署证据或凭据。模式扫描不能替代对新增文件和截图的人工检查。

归档校验只接受普通文件、Stored/Deflate 压缩及支持的标记；归档上限为 256 MiB，成员与整体摘要均分块读取。文件在校验期间被替换、截断或修改时，检查必须失败，不能复用之前的成功结论。

## 独立源码验收目录

对最终公开 `source-full.zip` 解包后的源码副本建立隔离依赖环境并执行完整回归；不要仅在包含私有配置、历史生成文件和预装依赖的旧工作目录中验收。解包时保留 ZIP 中记录的可执行权限，并将验收前后的公开源码清单与原 ZIP 逐项比对。生成的测试日志与截图不加入公开源码。

macOS 下，浏览器原生宿主若从 Downloads 等受限制目录启动，可能出现 `Operation not permitted`，而扩展只显示未连接。保留原始错误，在系统临时目录中的公开源码副本运行隔离浏览器测试；不要扩大浏览器权限、授予整盘访问、跳过用例或增加重试来掩盖失败。测试结束后记录副本路径、依赖版本、完整退出码与源码指纹。

## 初始化与发布仓库

使用公开包解压后的干净目录建立仓库，不要在混有私人资料的旧工作目录直接执行 `git add .`。检查暂存差异和文件列表，确认许可证、第三方声明、README 相对链接和执行权限完整。维护者确认仓库所有者、名称、可见性和 Git 提交身份后，才创建远程仓库并推送。

建议分支名 `main`。在 GitHub 启用私密漏洞报告、依赖安全告警与分支保护，并把 CI 设为合并条件；禁止把生产凭据暴露给 PR 工作流。工作流只检查和构建，不会创建 Release 或部署服务。

首次推送后必须核对 GitHub 中真实运行的 CI 结果；本机通过不能冒充远端 CI 通过。随后由维护者确认与 VERSION 对应的 `v${VERSION}` 标签、发布说明和经过校验的源代码包，再执行公开发布。准备完成与已经发布是两个不同状态。

## 发布后

在测试设备演练升级与回滚/恢复流程，再分批更新实际 Hub 和 Agent。分别核对运行版本、目录、服务名、数据完整性和权限。更新文档中的已知限制，不把未经验证的平台标记为支持验收完成。

## 构建与证据门禁

先按 [开发与验证](DEVELOPMENT.md) 安装哈希锁依赖，运行前端格式、类型检查、前端构建和资源清单生成。四片通过只能作为分片证据；每个平台必须合并完整收集清单和覆盖数据，所有测试恰好一次、零跳过、源码不变才算完整。diff coverage 是新增/修改可执行行的观察报告，不强加历史全仓 80% 门槛。

公开源码仅选择审阅后的当前指南；相对链接、README 当前版本、SECURITY 支持分支、生成资源内容哈希和 Compose 版本必须一致。LOCAL_RELEASE.json 留作历史本地记录，不参与当前版本判定，也不证明现用服务已更新。生产密钥轮换需要独立离线维护，不由发布任务触发。
