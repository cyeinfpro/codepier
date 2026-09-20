# CodePier · 码头

**CodePier 是一个自托管的远程开发桥接系统，用来把 AI、ChatGPT/MCP 客户端和你授权的本地开发环境连接起来。**

它由公网或内网可访问的 **Hub**、运行在开发电脑上的 **Agent**、浏览器管理面板、MCP 接口以及一组本地开发能力组成。Agent 主动连接 Hub，因此本地开发机不需要为了远程控制而开放入站端口。

## 项目定位

CodePier 解决的是「AI 在远端，本地代码和开发工具在自己的电脑上」这一类场景。

你可以把 Hub 部署在服务器上，在 Mac、Windows 或 Linux 开发机运行 Agent，然后按项目授权目录和能力。ChatGPT 或其他 MCP 客户端通过 Hub 操作这些已授权项目，而源码、CLI、浏览器登录态和本机工具仍留在 Agent 所在电脑。

典型使用场景包括：

- 在 ChatGPT 中直接读取、搜索、修改和检查本地项目代码。
- 远程执行项目命令、测试、构建、Git、SSH、Docker 等开发操作。
- 在网页面板中持续使用本机 Pi / Codex CLI 会话。
- 让长任务在浏览器关闭后继续运行，再从面板恢复状态和输出。
- 为不同项目配置不同的读写、命令、浏览器和桌面控制权限。
- 使用 Git worktree、语言服务、验收任务和交付物组织完整开发流程。
- 通过浏览器扩展使用已有浏览器登录态进行网页验证。
- 在明确授权后接入本机桌面控制能力。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 项目管理 | 将本地目录映射为项目，并按项目控制读写、任务、Shell 和其他能力。 |
| 文件与代码操作 | 文件读取、搜索、写入、移动、删除、批量补丁、SHA 校验和改动审阅。 |
| Shell 执行 | 在 Agent 电脑上执行真实开发命令，支持长任务、状态查询、输出恢复和取消。 |
| MCP 接口 | 向 ChatGPT / MCP 客户端提供项目、文件、任务、验收、交付物和开发工具能力。 |
| ChatGPT 接入 | 用简短文字返回结果；详细日志、任务和管理功能在面板查看。 |
| Pi / Codex 原生会话 | 从面板启动和管理本机原生 CLI，会话、输出和任务状态持续保留。 |
| 模型与思考设置 | 原生会话可读取本机 CLI 能力，并在支持时切换模型和思考强度。 |
| 文件与图片附件 | 为原生会话和项目导入受控附件，不需要把二进制文件塞进普通工具文本。 |
| Git 隔离工作区 | 基于真实 Git worktree 创建隔离开发目录，修改、测试和验收可绑定到同一工作区。 |
| 代码导航 | 接入本机语言服务，提供符号、定义、引用、悬停、诊断和调用关系等能力。 |
| 测试与验收 | 运行真实命令并保存源码指纹、退出码和输出，避免把过期测试结果当成当前结果。 |
| 任务与恢复 | 保存任务目标、步骤、操作编号和结果，在断线或页面关闭后继续查询原任务。 |
| 后台浏览器 | 通过浏览器扩展和原生消息宿主使用指定浏览器档案与站点授权进行网页验证。 |
| 桌面控制 | 在单独授权和本机权限满足时接入 Computer Use；可独立停止，不影响基础开发 Agent。 |
| Agent 生命周期 | 面板侧生成安装、更新、状态检查和卸载流程，保留已有配置和项目授权。 |
| 审计与诊断 | 保存操作状态、执行结果、错误和恢复信息，便于定位连接、权限与执行问题。 |

## 架构

```text
                    ┌──────────────────────────────┐
                    │ ChatGPT / MCP Client / Web   │
                    └──────────────┬───────────────┘
                                   │ HTTPS / MCP
                                   ▼
                    ┌──────────────────────────────┐
                    │           CodePier Hub       │
                    │                              │
                    │ Web Panel · Auth · MCP       │
                    │ Projects · Operations        │
                    │ Workflows · Artifacts        │
                    └──────────────┬───────────────┘
                                   │ Agent 主动连接
                 ┌─────────────────┼─────────────────┐
                 ▼                 ▼                 ▼
        ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
        │ Agent · Mac    │ │ Agent · Win    │ │ Agent · Linux  │
        ├────────────────┤ ├────────────────┤ ├────────────────┤
        │ Local Projects │ │ Local Projects │ │ Local Projects │
        │ Shell / Git    │ │ Shell / Git    │ │ Shell / Git    │
        │ Pi / Codex     │ │ Pi / Codex     │ │ Pi / Codex     │
        │ Local Tools    │ │ Local Tools    │ │ Local Tools    │
        └────────────────┘ └────────────────┘ └────────────────┘
```

### Hub

Hub 是中心服务，负责：

- 浏览器管理面板和 API。
- 登录认证、OAuth 与 MCP 接入。
- Agent 连接与设备状态。
- 项目映射和权限检查。
- 持久操作、任务、工作流和交付物状态。
- 原生 CLI 会话的远程控制入口。
- 操作日志、诊断和恢复信息。

Hub 不需要直接访问开发机的局域网地址。连接由 Agent 主动建立。

### Agent

Agent 运行在实际保存源码和开发工具的电脑上，负责：

- 访问明确授权的项目目录。
- 执行文件、Shell、Git 和构建操作。
- 管理 Pi / Codex 原生 CLI 进程。
- 调用配置好的语言服务。
- 管理浏览器桥接和可选桌面能力。
- 保留本机状态并把执行结果回传给 Hub。
- 在连接中断后重新连接并继续处理持久状态。

### Web 与 MCP Apps

`web/` 包含：

- CodePier 管理面板。
- 原生 CLI 会话界面。
- 开发工具和任务工作区。
- MCP Apps 自包含资源。
- 浏览器扩展及相关静态资源。

ChatGPT 默认使用简短文字回复，不自动插入项目、任务或改动卡片。工具仍返回完整的结构化结果，供模型读取代码、核对验证与交付物；详细日志和管理功能可在面板查看。MCP Apps 资源保留用于兼容已经打开的历史卡片。

## 快速开始

完整部署通常分为四步：

1. 部署 Hub。
2. 登录面板并创建 Agent 配对信息。
3. 在开发电脑安装 Agent，并授权项目目录。
4. 在 ChatGPT / MCP 客户端中连接 Hub。

## 部署 Hub

### 环境要求

Hub 安装脚本需要：

- Docker Engine
- Docker Compose 插件
- 主机 Python 3.9+

克隆项目后运行：

```bash
git clone https://github.com/cyeinfpro/codepier.git
cd codepier
bash install.sh
```

安装器会引导配置监听地址、端口和管理员账号。

无人值守安装可以使用密码文件：

```bash
bash install.sh \
  --host hub.example.com \
  --port 8765 \
  --username admin \
  --password-file /secure/admin-password \
  --non-interactive
```

常用环境变量示例位于 [`.env.example`](.env.example)。

### HTTPS

如果 Hub 暴露到公网，建议使用 HTTPS。仓库提供：

- `deploy/Caddyfile`
- `deploy/compose.https.yml`

反向代理来源应明确配置，不要把任意公网来源都视为可信代理。

## 安装 Agent

登录 CodePier 面板后，在设备管理中创建配对信息并使用面板生成的安装命令。

Agent 的设计原则是：

- 主动连接 Hub。
- 不要求在开发电脑开放公网入站端口。
- 项目目录需要显式授权。
- Shell、浏览器、桌面控制等高权限能力分别配置。
- 更新 Agent 时尽量保留已有配对、项目、CLI 和本机配置。

Agent CLI 入口：

```bash
./codepier agent --help
```

主要命令包括：

- `init`：导入 Hub 生成的配对信息。
- `run`：连接 Hub 并保持运行。
- `configure`：修改 Hub 地址和本地授权配置。
- `show`：查看当前配置摘要，不显示密钥。
- `computer-stop`：本机紧急停止桌面控制。
- `computer-resume`：解除桌面控制停止状态。

Agent 配置结构示例见 [`agent/config.example.json`](agent/config.example.json)。

## 项目与权限

CodePier 不是把整台开发机默认暴露给远端，而是以「项目」作为主要边界。

每个项目映射到 Agent 上的实际目录，并可以分别配置：

- 是否允许读取。
- 是否允许写入。
- 是否允许任务能力。
- 是否允许 Shell。
- 是否允许本机开发工具。
- 是否允许浏览器能力。
- 是否允许桌面控制。

文件工具还会对项目路径、目标文件、SHA 和工作区身份进行检查。项目名和工作区 ID 只用于定位，不能替代真正的权限验证。

## MCP 与 ChatGPT

Hub 提供 MCP 接口，可供 ChatGPT 或其他兼容 MCP 客户端连接。

CodePier 的 MCP 能力覆盖：

- 项目发现和工作区打开。
- 文件读取与搜索。
- 文件修改和批量补丁。
- 改动审阅。
- Shell 与持久操作。
- 任务与工作流。
- 验收结果。
- 交付物。
- Git worktree。
- 代码导航。
- 浏览器验证。
- 运行诊断。

对于需要多步执行的任务，服务端会保存操作编号和任务状态。客户端断线后应继续查询原操作，而不是因为一次网络超时就重新执行同一个写操作。

## Pi / Codex 原生会话

CodePier 可以从浏览器面板管理 Agent 电脑上的原生 Pi 和 Codex CLI。

原生会话与普通远程终端不同，它针对 AI CLI 的交互流程进行了单独管理，包括：

- 在指定项目目录启动会话。
- 保留会话历史和原生线程标识。
- 持续读取增量输出。
- 页面关闭后保持后台任务运行。
- 重新进入面板后恢复原会话。
- 发送文本、图片和文件。
- 在 CLI 支持时切换模型和思考强度。
- 停止指定会话而不影响其他项目。
- 管理和清理历史会话。

CodePier 不替换 Pi 或 Codex 的模型配置；它使用 Agent 电脑上已有的原生 CLI 和本地配置。

## 开发工具

面板中的开发工具用于把「查看代码 → 修改 → 测试 → 验收 → 交付」串成一条完整流程。

### 代码导航

可以配置本机语言服务，为已授权项目提供：

- 文件符号
- 项目符号
- 定义
- 引用
- Hover
- 诊断
- 调用方 / 被调用方

语言服务由用户在 Agent 电脑上明确安装和配置，CodePier 不会根据远端请求自动安装未知程序。

### Git Worktree

CodePier 可以基于已有 Git 仓库创建受管 worktree。

隔离工作区：

- 固定到明确的 Git 提交。
- 不复制原目录未提交修改。
- 不自动提交或合并。
- 文件修改、搜索、产物和验收都可以绑定到同一个工作区。
- 删除前检查工作区是否干净以及是否仍有活动任务。

### 测试与验收

验收功能可以运行真实命令，并保存：

- 执行命令。
- 工作目录。
- 退出码。
- 输出。
- 执行前后的源码指纹。
- 当前源码是否仍与验收时一致。

因此，一个历史测试即使曾经通过，在源码再次变化后也不会继续被当成当前版本的有效结果。

## 任务、操作与恢复

远程开发最容易出问题的地方不是一次命令失败，而是网络断开后无法确定「刚才到底执行了没有」。

CodePier 为耗时或有副作用的操作保存持久 operation：

```text
提交请求
   │
   ▼
operation_id
   │
   ├── queued
   ├── running
   ├── succeeded
   ├── failed
   └── cancelled
```

客户端拿到 `operation_id` 后可以持续查询同一个操作。网络超时不等于执行失败，也不需要盲目重放。

工作流还可以保存：

- 原始任务目标。
- 当前步骤。
- 已完成步骤。
- 未解决问题。
- 关联操作。
- 改动审阅。
- 验收记录。
- 交付物。

这使得较长的开发任务可以跨页面、跨连接继续处理。

## 后台浏览器

CodePier 提供可选浏览器桥接，用于网页调试和验证。

它由：

- Chrome / Chromium 扩展。
- Agent 本机原生消息宿主。
- Hub 与开发工具页面。

共同组成。

浏览器能力需要单独启用，并且按浏览器档案、项目和站点 origin 授权。远端操作使用预先准备的标签页，不会因为项目拥有文件写权限就自动获得浏览器权限。

适合的场景包括：

- 验证本地或测试环境网页。
- 查看真实浏览器渲染结果。
- 在已有登录态下检查后台页面。
- 执行受控的点击、输入、选择和滚动操作。

详细配置见 [开发能力说明](docs/INTEGRATIONS-20260917.md)。

## 桌面控制

Agent 包含独立的 Computer Use 接入层。

桌面控制与普通项目权限分开，需要：

1. Agent 配置显式启用。
2. 项目允许该能力。
3. 本机系统权限已经授予。
4. 当前调用者拥有对应权限。

本机可以单独停止桌面控制而不关闭 Agent：

```bash
./codepier agent computer-stop
```

恢复后仍会重新检查配置和系统权限：

```bash
./codepier agent computer-resume
```

## Agent 更新与维护

CodePier 的 Agent 生命周期包含：

- 安装
- 状态检查
- 更新
- 配置迁移
- 服务管理
- 卸载

相关实现主要位于：

- `scripts/agent_lifecycle.py`
- `shared/agent_lifecycle.py`
- `deploy/install-agent.sh`
- `deploy/install-agent.ps1`

面板生成的安装和维护流程会尽量保留已有配对、项目映射和状态，不需要每次升级重新配置整个 Agent。

## Hub 维护

Hub CLI：

```bash
./codepier hub --help
```

提供：

- `init`：初始化管理员。
- `run`：启动 Hub。
- `reset-password`：通过服务器本机权限重置账号。
- `backup`：一致性备份数据库和主密钥。

Hub 的数据目录包含认证、项目、操作和其他运行状态，应按私密数据处理。

## 在 ChatGPT 中用密码操作 VPS

用户授权服务器访问后，使用 `ssh_exec`，提供 `project`、`host`、`port`、`username`、`password`、远端 `command` 和 `idempotency_key`。Agent 自动执行非交互密码认证；本机需要 `ssh` 与 `sshpass`，仍须开启项目执行权限和完整 Shell 授权。`execution_info` 会报告依赖是否可用。

默认严格验证 SSH 主机密钥；首次连接可显式选择 `host_key_policy: "accept-new"`，保存新主机密钥，已有密钥变化仍拒绝。可用 `known_hosts_file` 指定 Agent 本机绝对路径。登录后的命令不支持交互输入或 sudo 密码提示。

已有本机部署脚本继续使用 `shell_exec`，并在 `env` 中明确传入 `SSHPASS`。只在聊天中写出密码不会自动设置环境变量；不要把密码拼进命令行。专用接口的密码不进入进程参数，审计摘要隐藏密码，输出流在持久化前遮盖精确密码值（不保证识别经编码或变形的密码）。排队请求按既有机制在 Hub 加密存储，完成后清除请求载荷。

两个接口都返回操作 ID。用 `operations_wait/get` 读取原操作结果，区分认证、主机密钥和命令失败；不要因连接中断重复执行。取消会停止本机 SSH 进程，不保证远端命令回滚或退出。

源码升级需同时更新 Hub 与 Agent，再在 ChatGPT 应用设置刷新工具目录，确认出现 `ssh_exec`。仅修改本地源码不会改变已运行的服务。

## 本地开发

推荐使用 Python 3.13。

创建开发环境：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-tools.txt
.venv/bin/python -m playwright install chromium webkit
```

MCP Apps 使用 Node.js 24：

```bash
npm --prefix web/mcp-apps ci --ignore-scripts
npm --prefix web/mcp-apps run build
```

构建集成资源：

```bash
.venv/bin/python scripts/build_integration_assets.py
```

运行代码检查：

```bash
.venv/bin/python -m ruff check agent hub shared scripts tests
```

运行完整回归：

```bash
.venv/bin/python scripts/check_full_regression.py \
  --output dist/regression \
  --workers 2
```

发布检查：

```bash
.venv/bin/python scripts/check_release.py
```

## VPS 管理

在面板保存服务器名称、IP / 域名、SSH 端口、账号和密码，再将连接分配给项目。同一 VPS 可供多个项目使用，一个项目也可分配多台 VPS；项目映射页面与 VPS 卡片都能调整分配。

ChatGPT 通过 `vps_list` 查询已授权连接，再通过 `vps_exec` 执行命令。例如：“SSH 到 Imago 的广州面板，检查磁盘占用。”模型只传项目、连接名称 / IP 与命令，不需要读取或重新传递已保存的密码。同 IP 多端口或账号必须明确选择。

密码使用 Hub 的 `master.key` 加密保存，由项目所属 Agent 执行时使用；项目分配不绕过执行权限、本机 Shell 授权或主机密钥验证。详见 [VPS 管理与调用](docs/VPS.md)。

## 项目目录

```text
codepier/
├── agent/                  # 本地 Agent、文件、Shell、CLI、浏览器、Computer Use
├── hub/                    # Hub、Web API、认证、MCP、工作流和持久状态
├── shared/                 # Hub / Agent 共用协议、契约、策略和工具
├── web/                    # 管理面板、原生 CLI UI、MCP Apps、浏览器扩展
├── deploy/                 # Docker、systemd、安装脚本和部署配置
├── scripts/                # 构建、迁移、诊断、验证和维护工具
├── skills/                 # CodePier 自带的开发工作流 Skills
├── tests/                  # 后端、浏览器、集成、迁移和回归测试
├── compose.yml             # Hub Docker Compose
├── Dockerfile              # Hub 镜像
├── install.sh              # Hub 安装 / 更新入口
├── codepier                # Unix CLI 入口
├── codepier.ps1            # Windows PowerShell 入口
└── RELEASE.json            # 源码版本信息
```

## 安全边界

CodePier 能执行真实文件修改、Shell、浏览器和桌面操作，因此权限配置应遵循最小授权原则。

建议：

- 只映射需要操作的项目目录。
- 不把密码、Token、配对文件提交到 Git。
- 公网 Hub 使用 HTTPS。
- Shell、浏览器和桌面能力按需要单独开启。
- 不把浏览器登录态或 Agent 配置复制到 Hub。
- 定期备份 Hub 数据。
- 升级前确认正在运行的长任务和原生 CLI 会话。
- 对重要写操作保留原 `operation_id` 和幂等键，避免网络异常后重复执行。

更多安全说明见 [SECURITY.md](SECURITY.md)。

## 相关文档

- [架构说明](docs/ARCHITECTURE.md)
- [VPS 管理与调用](docs/VPS.md)
- [开发能力与本机集成](docs/INTEGRATIONS-20260917.md)
- [开发工具工作流](docs/DEVTOOLS-FLOW-20260918.md)
- [ChatGPT MCP 工作区](docs/MCP-WORKSPACE-DASHBOARD-20260917.md)
- [发布流程](docs/RELEASING.md)
- [贡献指南](CONTRIBUTING.md)

## License

CodePier 使用 [MIT License](LICENSE)。

项目包含的第三方前端依赖保留各自许可证，相关文件位于 `web/vendor/` 和 `web/mcp-apps/THIRD_PARTY_NOTICES.txt`。
