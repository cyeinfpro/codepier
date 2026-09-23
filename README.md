# CodePier · 码头

**把 ChatGPT / MCP、浏览器管理面板和你自己的开发电脑连接起来，让 AI 在明确授权的项目边界内真正读取代码、修改文件、运行命令、管理原生 CLI、验证网页，并通过项目安全地操作已保存的 VPS。**

[![Version](https://img.shields.io/badge/version-1.12.0-2563eb)](RELEASE.json)
[![Python](https://img.shields.io/badge/Python-3.13%20recommended-3776ab)](requirements.txt)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](LICENSE)

> 当前仓库的 `RELEASE.json` 标记为 **1.12.0 / released / source-only**。该版本发布源码，不等同于已经部署到你的 Hub / Agent。

CodePier 面向这样的开发方式：

- AI 或 ChatGPT 在云端，但源码、CLI、浏览器登录态和开发工具在你自己的电脑上。
- 你希望远程开发能力以“项目”为边界，而不是默认暴露整台机器。
- 你需要让一次开发任务跨网络中断、页面刷新或长时间执行后仍然可以恢复。
- 你想在同一个面板里管理多个电脑、多个项目、Pi / Codex / Claude 原生会话，以及项目可用的 VPS。
- 你需要真实的测试、Git worktree、语言服务、浏览器验证和操作审计，而不是只让模型生成一段建议。

---

## 核心能力

| 能力 | 当前实现 |
| --- | --- |
| **项目级 MCP 开发** | 发现项目、读取/搜索代码、批量修改、SHA 校验、改动审阅、Shell、持久操作、任务、工作流、交付物和诊断。 |
| **全局 Pi / Codex / Claude 会话** | 在网页侧栏跨项目查看原生 CLI 会话，保留项目归属、状态、历史、草稿、附件、模型与思考设置。 |
| **开发工具工作流** | 从代码导航、Git worktree、测试验收到任务交接与网页验证，保持同一项目/工作目录范围。 |
| **持久操作与恢复** | 长任务返回 `operation_id`，支持排队、执行、查询、取消与断线后恢复，避免不确定结果被重复执行。 |
| **VPS 管理** | 在面板保存 VPS，支持 VPS ↔ 项目多对多分配；ChatGPT 通过 `vps_list` / `vps_exec` 使用已授权连接。 |
| **直接 SSH** | 对没有保存到 VPS 管理的临时服务器，可使用 `ssh_exec` 显式提供主机、账号和密码。 |
| **后台浏览器** | 通过浏览器扩展、原生消息宿主和项目级站点授权复用指定浏览器档案做网页观察与受控操作。 |
| **Computer Use** | 在独立本机授权、项目权限和系统权限都满足时启用桌面控制，并支持本机紧急停止。 |
| **Agent 生命周期** | 面板生成一键安装、修复升级、状态检查、升级和卸载流程，保留已有配对、项目授权与历史状态。 |
| **代码导航** | 接入本机语言服务，提供符号、定义、引用、Hover、诊断和调用关系。 |
| **Git 隔离工作区** | 基于真实 Git worktree 创建独立工作区，不复制原目录未提交修改，也不自动提交或合并。 |
| **测试与验收** | 保存真实命令、退出码、输出和源码指纹；源码变化后，历史通过结果不会继续伪装成当前版本通过。 |
| **审计与诊断** | 保存操作状态、错误、回执、恢复线索和执行上下文，便于定位权限、连接和执行问题。 |

---

## 工作方式

```text
                         ChatGPT / MCP Client
                                  │
                                  │ HTTPS / MCP
                                  ▼
                    ┌─────────────────────────┐
                    │      CodePier Hub       │
                    │                         │
                    │ Web Panel · Auth · MCP  │
                    │ Projects · Operations   │
                    │ CLI Sessions · VPS      │
                    └────────────┬────────────┘
                                 │
                         Agent 主动连接
                 ┌───────────────┼───────────────┐
                 ▼               ▼               ▼
        ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
        │ Agent · macOS  │ │ Agent · Windows│ │ Agent · Linux  │
        ├────────────────┤ ├────────────────┤ ├────────────────┤
        │ Local Projects │ │ Local Projects │ │ Local Projects │
        │ Shell / Git    │ │ Shell / Git    │ │ Shell / Git    │
        │ Native CLI     │ │ Native CLI     │ │ Native CLI     │
        │ Browser / LSP  │ │ Browser / LSP  │ │ Browser / LSP  │
        └───────┬────────┘ └───────┬────────┘ └───────┬────────┘
                │                  │                  │
                └──────── 已分配 VPS 由项目所属 Agent 发起 SSH ────────► VPS
```

### Hub

Hub 是中心服务，负责：

- 管理面板、登录认证、OAuth / MCP 接入。
- Agent 连接、设备状态与项目映射。
- 权限判断、持久操作、任务、工作流与交付物。
- 原生 CLI 会话的远程控制与同步状态。
- VPS 配置、项目分配与加密凭据存储。
- 审计、诊断、安装包和生命周期管理入口。

### Agent

Agent 运行在真正保存源码和开发工具的电脑上，负责：

- 访问明确授权的项目目录。
- 执行文件、Shell、Git、构建和测试操作。
- 管理本机 Pi / Codex / Claude 原生 CLI。
- 调用已配置的语言服务。
- 承接浏览器桥接和可选桌面控制。
- 从项目所在电脑发起 SSH / VPS 命令。
- 保留本机执行状态，并在网络恢复后继续与 Hub 同步。

**Agent 主动连接 Hub，所以开发电脑不需要为了 CodePier 开放公网入站端口。**

---

## 两条主要使用路径

### 1. 在 ChatGPT / MCP 中直接开发

典型流程：

```text
发现项目
  ↓
打开工作区
  ↓
读取 / 搜索源码
  ↓
基于文件 SHA 修改
  ↓
审阅差异
  ↓
执行测试 / 构建 / Git / Shell
  ↓
查询原 operation_id
  ↓
验证与交付
```

CodePier 不把“网络超时”直接当成“命令失败”。带副作用的长操作会返回持久 `operation_id`；客户端应继续查询原操作，而不是因为一次断线就重新执行。

代码结构和引用检索在独立解析进程中运行，最多同时启动两个解析进程。解析崩溃或超时会返回当前请求的错误回执，Agent 继续保持连接；超时进程会被停止回收。直接结构查询的解析预算为 15 秒，结构搜索单文件为最多 5 秒，并受搜索总预算约束。若同一个已开始的只读请求连续三次在 Agent 退出时未完成，会停止自动重试，保留错误回执供排查。

### 2. 在 Web 面板持续工作

Web 面板不只是设置页，也是一套远程开发工作区：

- **全局 CLI 会话**：侧栏直接显示当前有权限项目的 Pi / Codex / Claude 会话，不需要先逐个进入项目。
- **新建会话**：先搜索/选择项目，再选择 CLI、模型、思考强度和工作目录；发送首条消息时才真正创建会话。
- **离线历史**：Agent 离线时仍可浏览已经同步的历史记录。
- **附件与草稿**：文件、图片、草稿和上传状态绑定到对应项目/会话，不因切换聊天而串线。
- **开发工具**：从当前会话直接进入测试验收、任务交接、代码导航和网页验证。
- **运行诊断**：查看设备、项目、操作和能力就绪状态，不把“能力可调用”误写成“测试已经通过”。

---

## Claude Code 会话

在 CLI 会话设置中选择 **Claude**，即可使用节点本机 Claude Code 的流式消息、模型目录、图片、工具审批、交互提问、中断和原生历史恢复。节点需安装并完成 Claude Code 原生登录，Hub / Agent 均需更新到包含本功能的源码。思考强度在新建或恢复时设置；运行中支持排队跟进，不提供未经验证的即时补充。详见 [Claude Code 会话说明](docs/CLAUDE_CLI.md)。

## 面板一键更新

在 **系统设置 → 面板更新** 中检查 GitHub 正式 Release，并一键更新网页、Hub 和配套 Agent 安装文件。更新会验证源码包 SHA-256、预检候选镜像、复制完整数据卷，并在切换失败时尝试恢复原镜像和原数据卷；刷新页面或网络中断后仍可查看同一操作的进度。

首次需要在 Linux/systemd、Docker Compose 宿主机启用独立更新服务。部署目录建议使用 root 拥有且不可由其他用户写入的 `/opt/codepier`，在包含本功能的源码目录执行：

```bash
sudo bash install.sh --enable-panel-update
```

已运行新版面板的服务器也可单独执行 `sudo python3 scripts/panel_updater.py install --root "$PWD"`。网页进程不挂载 Docker socket，不能指定任意下载地址或宿主机命令。

**此入口更新服务器提供的 Agent 文件，不会强制升级或重启各台电脑上的 Agent。** 正式 Release 必须包含 `codepier-VERSION-source.zip` 及 GitHub SHA-256 元数据；没有合格的正式发布时会提示原因，不会直接执行 `main` 分支。支持范围、故障恢复及备份清理说明见 [面板更新文档](docs/PANEL_UPDATE.md)。

## VPS 管理

CodePier 1.10 起提供面板级 VPS 管理。

### 保存连接

在 **VPS 管理 → 添加 VPS** 中可以保存：

- 名称
- IP / 域名
- SSH 端口
- 用户名
- 密码
- 服务商 / 地区
- 系统与配置说明
- 备注

密码不会通过普通读取接口返回。编辑 VPS 时密码框保持为空，留空表示继续使用原密码。

### VPS ↔ 项目多对多分配

支持：

- **一台 VPS 分配给多个项目**
- **一个项目分配多台 VPS**

可以从 VPS 卡片管理项目，也可以从项目映射页管理该项目可用的 VPS。

VPS 分配不会绕过项目执行权限。真正执行命令时，仍需要：

- 项目当前授权有效。
- 项目允许执行相应能力。
- Agent 本机 Shell 能力已开启。
- 目标 Agent 具备 SSH 依赖。
- SSH 主机密钥验证通过。

### ChatGPT 使用已保存 VPS

先查询：

```text
vps_list(project="Imago")
```

再执行：

```text
vps_exec(
  project="Imago",
  vps="广州面板",
  command="df -h",
  idempotency_key="..."
)
```

因此可以直接对 ChatGPT 说：

> SSH 到 Imago 的广州面板，检查磁盘和服务状态。

模型只需要选择已经授权的连接，不需要再次读取或重新传递保存的密码。

同一 IP 存在不同端口或账号时，CodePier 要求明确选择，不会自动对多个目标执行命令。

### 主机密钥与凭据

- 默认使用严格 SSH 主机密钥校验。
- 首次连接可以显式选择 accept-new；后续密钥变化仍会拒绝。
- VPS 密码使用 Hub 的 `master.key` 加密保存。
- 排队操作保存连接引用，不把密码写进普通任务参数。
- Agent 执行时通过受控环境传递密码，不把密码放进 SSH 命令参数。
- Hub 数据库和对应 `master.key` 必须一起备份。

完整说明见 [VPS 管理与 SSH 调用](docs/VPS.md)。

---

## 临时服务器：直接 SSH

如果服务器没有保存进 VPS 管理，也可以使用 `ssh_exec` 直接连接。

该路径适合一次性或临时主机，需要在调用时明确提供：

- 项目
- 主机 / IP
- SSH 端口
- 用户名
- 密码
- 命令
- 幂等键

`ssh_exec` 与 `vps_exec` 都复用 CodePier 的持久操作、超时、取消、主机密钥校验和结果恢复机制。

> 对经常使用的服务器，优先保存到 VPS 管理并分配给项目；这样后续调用不需要反复在聊天中传递密码。

---

## 原生 Pi / Codex / Claude 会话

CodePier 可以从浏览器面板管理 Agent 电脑上的原生 Pi、Codex 和 Claude Code CLI。

当前会话工作流包括：

- 跨项目全局会话列表。
- 项目、状态与关键词筛选。
- 在指定项目和子目录创建会话。
- 页面关闭后继续运行后台任务。
- 重新进入后恢复已同步历史和会话状态。
- 发送文本、文件和图片附件。
- 在 CLI 支持时读取/切换模型和思考强度。
- 模型目录失败后显式刷新，不要求刷新整个页面。
- 停止、恢复、删除网页记录、导出等状态化操作。
- 保留原生线程/会话标识，不把网页记录伪装成本机 CLI 数据本身。

CodePier 不替换 Pi / Codex / Claude 自己的账号、模型配置或本地认证；它使用 Agent 电脑上已经安装和配置的原生 CLI。

---

## 开发工具

面板中的 **开发工具** 把“查看代码 → 修改 → 测试 → 验收 → 交付”串成一个连续工作流。

### 代码导航

可接入本机语言服务，提供：

- 文件符号
- 工作区符号
- 定义
- 引用
- Hover
- 诊断
- 调用方 / 被调用方

CodePier 不会因为一次远程请求就自动安装未知语言服务器。

### Git worktree

受管 worktree：

- 从明确 Git 提交创建。
- 不复制原目录未提交修改。
- 不自动提交。
- 不自动合并。
- 文件操作、搜索、测试和验收可绑定到同一个隔离工作区。
- 删除前检查工作区是否干净以及是否还有活动任务。

### 测试与验收

验收记录保存：

- 实际执行命令
- 工作目录
- 退出码
- 输出
- 执行前后源码指纹
- 当前源码是否仍与验收时一致

所以一次历史测试即使曾经通过，只要源码之后发生变化，就不会继续被当成当前版本的有效通过。

### 任务交接

任务/工作流可以保存：

- 原始目标
- 已完成步骤
- 当前断点
- 未解决问题
- 关联 operation
- 改动审阅
- 验收记录
- 交付物

适合把长任务从 ChatGPT、开发工具页或 CLI 会话继续接起来。

---

## 后台浏览器

CodePier 的浏览器能力由以下部分组成：

- Chrome / Chromium 扩展
- Agent 本机原生消息宿主
- Hub / 开发工具页面

浏览器权限与普通文件权限分开，需要按项目、浏览器档案和站点 origin 授权。

适合：

- 验证本地或测试环境网页。
- 读取真实浏览器渲染后的页面。
- 在已有登录态下检查后台系统。
- 执行受控的点击、输入、选择和滚动。

网页内容始终作为不可信输入处理。观察页面不会自动获得提交、删除、购买或发送等业务权限。

详见 [开发能力与本机集成](docs/INTEGRATIONS-20260917.md)。

---

## Computer Use

桌面控制是独立能力，不会因为项目有文件读写权限就自动开启。

启用需要同时满足：

1. Agent 配置明确启用。
2. 项目允许桌面控制。
3. 本机系统权限已授予。
4. 当前调用者拥有对应授权。

本机可紧急停止桌面能力而不关闭基础 Agent：

```bash
./codepier agent computer-stop
```

恢复：

```bash
./codepier agent computer-resume
```

---

## 快速开始

完整链路通常是四步：

1. 部署 Hub。
2. 登录面板并接入一台开发电脑。
3. 创建项目映射并设置权限。
4. 将 ChatGPT / MCP 客户端连接到 Hub。

### 1. 部署 Hub

Hub 安装脚本需要：

- Docker Engine
- Docker Compose 插件
- 主机 Python 3.9+

```bash
git clone https://github.com/cyeinfpro/codepier.git
cd codepier
bash install.sh
```

无人值守安装：

```bash
bash install.sh \
  --host hub.example.com \
  --port 8765 \
  --username admin \
  --password-file /secure/admin-password \
  --non-interactive
```

环境变量示例见 [`.env.example`](.env.example)。

如果 Hub 暴露到公网，建议使用 HTTPS。仓库包含：

- `deploy/Caddyfile`
- `deploy/compose.https.yml`

### 2. 安装 Agent

登录面板，在 **设备节点 → 接入电脑** 中选择系统、填写设备信息和授权目录，然后复制面板生成的一键安装命令到目标电脑执行。

设计原则：

- Agent 主动连接 Hub。
- 开发电脑无需开放公网入站端口。
- 授权目录必须显式配置。
- Shell、浏览器、桌面等高权限能力分别开启。
- 更新尽量保留设备身份、项目授权、历史和本机状态。

默认受管安装目录：

```text
~/.codepier-agent
```

Agent CLI：

```bash
./codepier agent --help
```

常用入口包括：

- `init`
- `run`
- `configure`
- `show`
- `computer-stop`
- `computer-resume`

详细安装、升级和卸载见 [Agent 安装说明](docs/AGENT_INSTALL.md)。

Windows 安装完成后，Agent 使用无窗口后台任务运行，关闭终端不会退出；系统开机时无需登录桌面即可启动。安装器会请求一次 UAC 授权来注册开机任务，Agent 保持原安装账户的普通权限。进程退出后每分钟自动补拉，事件循环持续卡死 120 秒会退出并由系统重启，网络断线持续重连。升级和卸载期间自动补拉会暂停。

旧版 Windows 源码安装先关闭前台 Agent 窗口，再运行新版 `deploy\start-agent.cmd`，会保留原配对、项目授权和历史并转入受管目录。旧版受管安装在本机执行新版升级命令后接受 UAC；随后可用以下命令修复后台启动：

```powershell
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" start
```

日志位于 `%USERPROFILE%\.codepier-agent\logs`。此模式采用 S4U 非交互登录，不保存 Windows 密码，也不提供交互桌面；依赖桌面登录的映射盘、Windows 集成网络认证或加密凭据需要另外配置。[Windows 任务安全上下文](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)

Windows 原生恢复验收：在管理员终端运行 `python scripts/check_windows_service.py --output dist/windows-service.json`。测试只创建临时任务，验证关闭启动进程后常驻、进程被杀或卡死后的恢复，以及维护期间不被补拉，最后清理临时任务。它不重启或注销电脑，重启后未登录时的上线情况需要在目标节点实测。

### 3. 创建项目映射

CodePier 以“项目”作为主要授权边界。

每个项目映射到某台 Agent 上的实际目录，并可分别控制：

- 读取
- 写入
- 任务
- Shell / 执行
- 本机开发工具
- 浏览器
- 桌面控制

项目名和工作区 ID 只负责定位，不能替代真正的权限检查。

### 4. 连接 ChatGPT / MCP

Hub 提供 MCP 接口。根据部署环境，可使用：

- 公开 HTTPS + OAuth
- 官方 Secure MCP Tunnel + 本项目 stdio 桥接器

完整步骤见 [连接 ChatGPT](docs/CHATGPT.md)。

---

## Agent 生命周期管理

CodePier 的受管 Agent 支持：

- 一键安装
- 已安装环境的修复升级
- 状态检查
- 版本更新
- 服务管理
- 受控卸载
- 旧命名 / 路径兼容迁移

重复执行同一节点的新安装命令时，会先核对设备身份和现有受管安装，再进入修复/升级流程，而不是简单提示“已经安装”。

新版受管安装还会生成本机维护入口。Linux / macOS 示例：

```bash
"$HOME/.codepier-agent/codepier-agent" status
"$HOME/.codepier-agent/codepier-agent" upgrade
"$HOME/.codepier-agent/codepier-agent" uninstall
```

维护操作不会自动删除项目源码。卸载范围、升级回滚和旧版本迁移边界见：

- [Agent 安装、升级与卸载](docs/AGENT_INSTALL.md)
- [Agent 生命周期](docs/AGENT_LIFECYCLE.md)
- [CodePier 命名迁移](docs/CODEPIER-MIGRATION.md)

---

## 持久操作与幂等

对耗时或有副作用的操作，CodePier 使用持久 operation：

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

客户端应：

- 保存原 `operation_id`。
- 等待时继续查询同一操作。
- 响应丢失时按原幂等键查找原操作。
- 不因为一次网络错误就生成新的写操作。
- 取消后仍理解“停止本地传输”不等于“远端一定回滚”。

这套机制也被 Shell、SSH、VPS 和测试验收等功能复用。

---

## 安全模型

CodePier 可以执行真实文件修改、Shell、浏览器和桌面操作，因此默认设计重点是**明确授权与可恢复审计**，而不是把远程执行伪装成沙箱。

建议：

- 只映射实际需要操作的项目目录。
- 按项目分别开启 read / write / execute 等能力。
- 不把密码、Token、配对文件或 Agent 配置提交到 Git。
- 公网 Hub 使用可信 HTTPS。
- Shell、浏览器和 Computer Use 按需开启。
- 不把浏览器登录态复制到 Hub。
- 定期备份 Hub 数据库及其对应主密钥。
- Agent 生命周期维护前先确认没有关键长任务或原生 CLI 会话。
- 对重要写操作保存 `operation_id` 和幂等键。
- SSH 默认保持主机密钥校验，不通过关闭校验解决连接问题。

更多说明见 [SECURITY.md](SECURITY.md)。

---

## Hub 维护

Hub CLI：

```bash
./codepier hub --help
```

主要能力包括：

- `init`：初始化管理员
- `run`：启动 Hub
- `reset-password`：通过服务器本机权限重置账号
- `backup`：一致性备份数据库和主密钥

Hub 数据目录包含认证、项目、操作、VPS 凭据和其他运行状态，应按私密数据处理。

---

## 本地开发

推荐使用 Python 3.13。

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

代码检查：

```bash
.venv/bin/python -m ruff check agent hub shared scripts tests
```

完整回归：

```bash
.venv/bin/python scripts/check_full_regression.py \
  --output dist/regression \
  --workers 2
```

发布检查：

```bash
.venv/bin/python scripts/check_release.py
```

---

## 项目结构

```text
codepier/
├── agent/                  # 本机 Agent、文件、Shell、CLI、浏览器、Computer Use
├── hub/                    # Hub、Web API、认证、MCP、工作流、VPS 与持久状态
├── shared/                 # Hub / Agent 共用协议、契约、策略和工具
├── web/                    # 管理面板、CLI UI、开发工具、VPS UI、浏览器扩展
├── deploy/                 # Docker、systemd、安装器和部署配置
├── scripts/                # 构建、迁移、诊断、验证和维护工具
├── skills/                 # CodePier 自带工作流 Skills
├── tests/                  # 后端、浏览器、集成、迁移和回归测试
├── compose.yml             # Hub Docker Compose
├── Dockerfile              # Hub 镜像
├── install.sh              # Hub 安装 / 更新入口
├── codepier                # Unix CLI
├── codepier.ps1            # Windows PowerShell CLI
├── CHANGELOG.md            # 版本变更
└── RELEASE.json            # 当前源码版本状态
```

---

## 文档索引

- [开始使用](docs/START-HERE.md)
- [架构说明](docs/ARCHITECTURE.md)
- [连接 ChatGPT](docs/CHATGPT.md)
- [VPS 管理与 SSH 调用](docs/VPS.md)
- [Agent 安装、升级与卸载](docs/AGENT_INSTALL.md)
- [Agent 生命周期](docs/AGENT_LIFECYCLE.md)
- [CodePier 命名迁移](docs/CODEPIER-MIGRATION.md)
- [开发工具工作流](docs/DEVTOOLS-FLOW-20260918.md)
- [全局 CLI 会话流程](docs/CLI-GLOBAL-FLOW-20260918.md)
- [开发能力与本机集成](docs/INTEGRATIONS-20260917.md)
- [Computer Use](docs/COMPUTER_USE.md)
- [发布流程](docs/RELEASING.md)
- [安全说明](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

---

## 当前版本状态

仓库当前版本信息来自 [`RELEASE.json`](RELEASE.json)：

- **Version:** 1.12.0
- **Date:** 2026-09-23
- **Status:** released
- **Source only:** true
- **Published:** true
- **Deployed:** false

具体版本变化见 [CHANGELOG.md](CHANGELOG.md)。

---

## License

CodePier 使用 [MIT License](LICENSE)。

项目包含的第三方前端依赖保留各自许可证，相关声明位于 `web/vendor/` 和 `web/mcp-apps/THIRD_PARTY_NOTICES.txt`。
