# CodePier · 码头

让 AI 与你授权的本地代码安全对接。CodePier 由远程 Hub 面板、本地 Agent、MCP 接口及浏览器工作区组成；Agent 主动连接 Hub，本地电脑无需开放入站端口。

当前源码版本：**1.9.1**。`RELEASE.json` 描述源码候选版本，不代表已经部署或已经发布到 GitHub。实际运行版本与磁盘源码分别展示，修改源码后应按运维流程重启对应组件。

## 能做什么

CodePier 提供项目与受管工作区、带 SHA 校验的文件编辑和批量补丁、可恢复的操作记录、代码审查与验证结果、跨项目 CLI 会话和持久工作流。浏览器、桌面和本机集成能力需要独立授权，不会因授予文件读取权限而自动开启。

Hub 保存账号、授权、操作与工作流状态；源码和原生开发工具保留在 Agent 电脑上。网络断开后的操作状态需要根据持久记录恢复，不能把连接失败当成执行失败后盲目重复写入。

## 安装 Hub

准备 Docker Engine、Docker Compose 插件及主机 Python 3.9+。下载并校验源码包后，在源码目录运行：

```bash
bash install.sh
```

安装器会询问主机、端口和管理员信息。无人值守安装使用私密密码文件，不要把密码写进命令行参数或提交到仓库：

```bash
bash install.sh --host hub.example.com --port 8765 \
  --username admin --password-file /secure/admin-password --non-interactive
```

首次初始化应通过可信网络或 SSH 转发进行。公网访问必须配置 HTTPS，并只信任实际反向代理的来源地址；不要将 `FORWARDED_ALLOW_IPS` 设置为任意来源。HTTP 无法保护浏览器密码或配对文件。

安装脚本管理 Compose 使用的外部数据卷。首次安装不要跳过它而直接执行 `docker compose up`；更新前备份数据并检查安装器的迁移结果。存在自定义数据挂载或新旧安装冲突时，安装器会停止，而不是创建空数据冒充升级成功。

## 安装与管理 Agent

登录面板后，在设备管理中生成安装指令并在目标电脑执行。安装票据和配对文件是机密，不要贴到公开 Issue。只开放实际需要的项目目录和能力。

Agent 的安装、更新、服务状态与卸载由面板生成的管理指令和 `scripts/agent_lifecycle.py` 处理。升级会识别旧版命名并迁移受管理的路径、服务及配置；旧名称仍可作为迁移输入。不要手工全局替换协议标识、数据库字段或持久键值。

详细能力设置见 [集成指南](docs/INTEGRATIONS-20260917.md)、[开发能力流程](docs/DEVTOOLS-FLOW-20260918.md) 和 [MCP 工作区](docs/MCP-WORKSPACE-DASHBOARD-20260917.md)。

## 本地开发

开发与发布验证使用 Python 3.13；构建 MCP Apps 需要 Node.js 24。以下环境与正在运行的生产 Hub/Agent 应分开：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-tools.txt
.venv/bin/python -m playwright install chromium webkit
python3.13 -m venv .venv-compat
.venv-compat/bin/python -m pip install --upgrade pip==26.2.1
.venv-compat/bin/python -m pip install -r requirements-compat.txt
npm --prefix web/mcp-apps ci --ignore-scripts
npm --prefix web/mcp-apps run build
.venv/bin/python scripts/build_integration_assets.py
```

开发面板采用独立数据目录。`hub init` 会安全地读取密码：

```bash
export HUB_DATA_DIR="$PWD/.work/local-hub"
export HUB_PUBLIC_URL="http://127.0.0.1:8765"
./codepier hub init --username admin
./codepier hub run --host 127.0.0.1 --port 8765
```

运行检查：

```bash
.venv/bin/python -m ruff check agent hub shared scripts tests
.venv/bin/python scripts/check_full_regression.py --output dist/regression --workers 2
.venv/bin/python scripts/check_release.py
```

每次完整回归应使用新的输出目录。报告记录实际收集的测试、退出码、跳过项、超时和源码变化；不能把部分测试通过或旧报告当成本次完整验收。真实原生 CLI、操作系统安装服务与 Docker 迁移探针应在隔离环境中单独运行，不属于“自动启动外部模型”的许可。

## 安全与发布

文件写入、命令执行、浏览器及桌面操作具有真实副作用。请阅读 [安全边界](SECURITY.md)，按最小权限授权。安全问题请私下披露；公开反馈中移除密码、令牌、配对文件、私人源码和服务地址。

```bash
.venv/bin/python scripts/build_source_bundle.py --public --output dist/codepier-1.9.1-source.zip
.venv/bin/python scripts/check_release.py --bundle dist/codepier-1.9.1-source.zip
```

公开包包含逐文件 SHA-256 清单、执行权限及第三方许可，不包含 Agent 状态备份、运行数据库、安装环境或历史本机验收记录。GitHub 发布步骤见 [发布清单](docs/RELEASING.md)；贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

项目采用 [MIT License](LICENSE)。保留原作者和贡献者版权声明；第三方资源使用各自的许可证，见 `web/vendor` 中的许可文件及 `web/mcp-apps/THIRD_PARTY_NOTICES.txt`。
