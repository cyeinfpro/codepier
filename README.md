# CodePier · 码头

让 ChatGPT 通过 MCP 插件使用你的本地项目：读代码、改文件、跑测试，也能接入浏览器、桌面和远程服务器。

[![Version](https://img.shields.io/badge/version-1.17.0-2563eb)](RELEASE.json)
[![Python](https://img.shields.io/badge/Python-3.13-3776ab)](requirements.txt)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](LICENSE)

CodePier 是自托管的开发工具连接层。**Hub** 提供 MCP 接口和管理面板，**Agent** 在你的电脑上访问文件、运行命令。接入 ChatGPT 后，可以直接围绕真实项目完成检查、修改和验证；也可以在网页里继续本机的 Pi、Codex 或 Claude Code 会话。

项目不需要先搬到云端，开发电脑也不需要开放公网入站端口。代码、Git 和命令行工具仍在原来的机器上运行；被读取的内容、执行结果和会话记录会按使用流程传递给 Hub 或客户端。

[快速开始](#快速开始) · [日常使用](#日常使用) · [权限与数据](#权限与数据) · [更新与备份](#更新与备份) · [文档](#文档) · [版本发布](https://github.com/cyeinfpro/codepier/releases)

## 可以用它做什么

| 场景 | 功能 |
| --- | --- |
| 在 ChatGPT 中处理本地项目 | 读取和搜索源码、按 SHA 校验修改文件、审阅差异、运行命令与具名任务，保留执行回执。 |
| 查看项目和任务进度 | 只读查询目录、Skills、权限与操作状态；在支持 MCP Apps 的客户端主动打开工作台、审阅改动和选择上下文。 |
| 在浏览器中继续开发 | 跨项目查看 Pi / Codex / Claude 会话，选择模型与工作目录，发送文件和图片，处理工具审批，恢复已同步历史。 |
| 管理多台开发电脑和服务器 | 按设备映射项目；保存 VPS 连接并分配给项目，由项目所在的 Agent 发起 SSH。 |
| 验证修改结果 | 使用 Git worktree 隔离工作目录，查询本机语言服务，运行绑定源码版本的验收，验证真实网页。 |
| 和团队一起使用 | 个人与团队 Space、成员邀请、OIDC 登录、动态角色及私有 Access Profile；按需共享工作流和交付物。 |
| 接入其他 MCP 服务 | 在同一入口使用已审核的外部工具，按后端账号、角色规则和每个连接的明确同意控制访问。 |
| 排查一次调用 | 查看输出、返回结果、退出码和执行阶段；断线后继续查询原操作，使用请求关联 ID 辅助排障。 |
| 只读多智能体协作（main 试点） | 独立讨论室、明确收件人、建议审批、持久任务与固定 HTTP 探针；MCP Events 服务端适配默认关闭，真实 Work/dot 路由仍需单独验收。 |
| 维护安装环境 | 面板检查正式 Release 并更新 Hub；Agent 支持安装、修复升级、状态检查和卸载。 |

CodePier 不提供模型服务，也不代替原生 CLI 的账号和订阅。Hub 与 Agent 本身不需要模型 API Key；Pi、Codex、Claude Code 的认证和费用由各自的配置决定。只使用 MCP 文件与命令工具时，不必安装这些 CLI。

### 协作中心只读试点

main 新增独立「协作中心」：面板指令、MCP 建议与监控异常共享目标、任务和证据。所有新功能默认关闭，不自动创建 ChatGPT 聊天、订阅或启用生产采集，也不授予代码修改和部署权限。当前交付固定 HTTP 探针与服务端 Events 适配，不包含分布式主机监控或自动代码修复；尚未作为新的正式 Release 发布。

使用、功能开关和停止流程见[协作中心说明](docs/COLLABORATION.md)，设计调整及边界见[实施设计 v0.2](docs/designs/COLLABORATION_V0_2.md)。有效订阅不代表模型在线，分析完成不代表业务恢复。

## 工作方式

```text
ChatGPT / MCP 客户端                 浏览器管理面板
          │                              │
          └────────── HTTPS ─────────────┘
                         │
                    CodePier Hub
              认证 · 项目 · 会话 · 操作记录
                         ▲
                         │ Agent 主动连接
             ┌───────────┼───────────┐
             │           │           │
          macOS       Windows       Linux
          Agent        Agent        Agent
             │           │           │
          本机项目     本机项目      本机项目
          Git / CLI    Git / CLI    Git / CLI
             └────── SSH 到已分配的 VPS ──────►
```

**Hub** 负责面板、认证、项目映射、MCP 接入和操作记录。**Agent** 负责实际访问文件、启动本机工具和执行命令。一台 Hub 可以连接多台 Agent，每个项目对应其中一台电脑上的实际目录。

项目映射限制文件工具的访问范围，但**完整 Shell 不是项目目录沙箱**：命令使用 Agent 运行账号的系统权限。启用执行权限前，请确认该账号能够访问哪些文件和凭据。

## 平台与运行环境

| 部分 | 当前实现与要求 |
| --- | --- |
| Hub | Docker Compose 部署，应用使用 Python 3.13；提供 Linux amd64 / arm64 镜像和 k3s 单副本清单。 |
| Agent | macOS、Windows、Linux 安装与维护入口；安装器准备独立 Python 3.13 环境。 |
| 原生 CLI | 复用 Agent 账号下已安装并登录的 Pi、Codex、Claude Code；具体能力取决于 CLI 和平台。 |
| 浏览器 | 需要配置本机扩展、原生消息宿主、浏览器档案和网站授权。 |
| 桌面控制 | 独立授权的可选能力；当前已验证的原生提供方平台为 macOS，Windows/Linux 桌面后端尚未验证。 |
| 开发与测试 | Python 3.13、Node.js 24；Linux/macOS 分片完整回归，Windows 运行明确的核心与后台恢复子集。 |

平台代码和 CI 覆盖不等于每台真实设备都完成了安装、重启与升级验收。系统服务、浏览器及桌面权限仍需在目标机器检查，见 [Agent 安装](docs/AGENT_INSTALL.md)、[Computer Use](docs/COMPUTER_USE.md) 和[开发与验证](docs/DEVELOPMENT.md)。

## 快速开始

首次使用按“部署 Hub → 接入电脑 → 添加项目 → 连接客户端”的顺序完成。需要 HTTPS 配置、逐步检查或遇到错误时，参照[完整入门教程](docs/START-HERE.md)。

### 1. 部署 Hub

服务器需要 Git、Bash、Docker Engine、Docker Compose v2，以及供安装脚本使用的 **Python 3.9+**。Compose 需要支持 `up --wait`；应用运行环境由 Docker 镜像提供，使用 Python 3.13。

下面以正式版本 `v1.17.0` 为例。请在可信网络中完成 HTTP 初始安装；公网部署应先按[入门教程](docs/START-HERE.md)配置 HTTPS，不要通过裸公网 HTTP 输入密码或配对设备。

```bash
git clone --branch v1.17.0 --depth 1 https://github.com/cyeinfpro/codepier.git
cd codepier
bash install.sh
```

安装器会询问服务器地址、端口和管理员账号，构建镜像并初始化数据。管理员密码由你设置，没有默认密码。完成后检查：

```bash
docker compose ps
curl --fail http://127.0.0.1:8765/healthz
```

这里的 `8765` 是默认端口；修改过端口时请相应替换。浏览器打开安装时配置的面板地址，使用刚创建的账号登录。日志可通过 `docker compose logs --tail=100 hub` 查看。

也可以从 [Releases](https://github.com/cyeinfpro/codepier/releases) 下载源码包。手动安装和开发建议使用 `codepier-VERSION-source-full.zip`；`source.zip` 用于兼容面板更新器。环境变量见 [`.env.example`](.env.example)。

使用预构建镜像时，正式版本地址为 `ghcr.io/cyeinfpro/codepier:1.17.0`，生产部署建议固定镜像摘要。k3s 单文件清单见 [`deploy/k3s/deployment.yaml`](deploy/k3s/deployment.yaml)；Compose 可通过 `CODEPIER_HUB_IMAGE` 选择镜像，首次安装与数据初始化仍应遵循[入门教程](docs/START-HERE.md)。OIDC 环境变量播种和首次 SSO 管理员初始化需显式配置，见[多用户/OIDC 指南](docs/MULTIUSER_OIDC.md)。

### 2. 接入开发电脑

在面板打开 **设备节点 → 接入电脑**，选择系统，填写设备名称、这台电脑能访问的 Hub 地址，以及一个已经存在的绝对授权目录。复制生成的命令，在目标电脑的本机终端执行。

授权目录可以是 `/Users/me/Projects`、`/home/me/Projects` 或 `D:\Projects`。安装器会准备独立的 Python 3.13 环境、校验 Agent 安装包、配对设备并配置自动启动。默认安装目录为当前用户的 `~/.codepier-agent`。

新安装默认勾选 **Shell 与目录任务**，只需要文件访问时可以取消。浏览器与桌面控制需要另外配置；升级已有 Agent 会保留原来的权限设置，不会自动开启以前关闭的能力。

安装命令包含短期有效的一次性票据，不要公开分享。完成后应在设备列表看到节点在线。各系统的安装、恢复和维护步骤见 [Agent 安装指南](docs/AGENT_INSTALL.md)。

### 3. 添加项目

在 **项目映射** 中添加项目，选择刚接入的设备，为项目设置名称和实际路径。例如：

```text
项目名称：demo
所属设备：你的开发电脑
项目路径：/Users/me/Projects/demo
```

项目路径必须位于 Agent 已授权的目录内。先开启读取权限，确认能够打开项目中的一个普通文件；需要修改代码、运行命令时，再开启对应的写入和执行能力。

“Agent 在线”和“项目可执行”是两回事。运行命令需要 Agent 本机能力、面板项目设置和客户端授权同时允许，不能只打开其中一个开关。

### 4. 连接 ChatGPT 或其他 MCP 客户端

公开接入地址为：

```text
https://hub.example.com/mcp
```

先在 **系统设置 → 公开地址** 保存实际 HTTPS 基地址，不加 `/mcp`。在 ChatGPT 的 Plugins 中添加 MCP 连接，填写完整 MCP URL，按 CodePier 的 OAuth 流程登录并确认项目范围和工具权限。客户端入口、开发者设置和账号条件以 [OpenAI 官方说明](https://developers.openai.com/plugins/deploy/connect-chatgpt)为准；CodePier 配置见[接入教程](docs/CHATGPT.md)。

连接完成后，在新对话中启用 CodePier，先尝试一个只读请求：

> 打开 CodePier 的 demo 项目，读取 README，说明项目结构和测试入口。先不要修改文件或执行安装命令。

不公开 Hub 时，可以使用 **Secure MCP Tunnel + stdio 桥接器**；支持 Bearer 认证的其他 MCP 客户端也可以使用面板创建的限定范围 PAT。配置方法见[连接指南](docs/CHATGPT.md)。只想在面板里使用原生 CLI 会话，可以跳过 MCP 客户端接入。

## 日常使用

### 在 ChatGPT 中修改代码

直接说明项目、目标和执行边界即可。例如：

> 在 demo 项目中检查登录失败的原因。先复现问题，再做最小修改并运行相关测试，最后列出修改文件和验证结果，不要提交或部署。

一个完整的流程通常是：打开项目并读取说明，确认现有改动，读取或搜索相关源码，修改后审阅差异，再运行测试。涉及多个步骤的工作可以保存为工作流，记录目标、进度、未解决问题和关联操作，便于之后继续。

耗时操作会返回 `operation_id`。页面刷新或网络中断后，用 `task_query` 继续查询原编号；明确取消使用 `process`。不要换一个幂等键重跑结果不明的命令。是否完成要看终态、退出码和实际输出。

### MCP 工具

当前原生目录由九个核心开发工具、两个只读查询和两个身份工具组成；外部网关工具按授权另行提供。

| 入口 | 用途 |
| --- | --- |
| `project_query` | 只读发现项目，查询目录、Skills、工具帮助、权限、配置任务和工作流；不捕获改动基线。 |
| `task_query` | 读取、等待和追踪原操作回执，不执行、取消或重跑命令。 |
| `get_profile` / `get_access_context` | 确认当前连接的稳定身份、项目范围及有效权限。 |

九个核心开发工具：

| 工具 | 用途 |
| --- | --- |
| `workspace` | 项目发现、工作区、Skills、工作流和能力说明。 |
| `read` | 文件、改动记录、交付物、符号和语言服务查询。 |
| `write` | 写入文件、导入附件、登记交付物。 |
| `edit` | 精确编辑、批量修改、检查点和恢复。 |
| `exec` | 本机命令、已配置任务和保存的 VPS 命令。 |
| `process` | 查询或取消操作、搜索、验收和执行诊断。 |
| `vps` | 查询当前项目可用的服务器连接。 |
| `browser` | 打开、观察和操作已授权网页。 |
| `computer` | 在独立授权下观察和操作本机应用。 |

只读发现和专项参数优先使用 `project_query` 的 `help` 操作；捕获基线、创建 worktree 或更新工作流仍使用 `workspace`。文件修改使用读取时取得的 SHA 检查冲突；新建文件使用 `expected_sha256="new"`。`exec` 是非交互式命令入口，不提供 PTY/stdin 会话。独立命令可以并行，访问相同文件或服务时应显式声明资源；这些锁不约束外部编辑器或人工操作。

ChatGPT 内的项目选择和任务看板已移除；直接在聊天中使用工具即可。项目上下文、保存的工作流、执行证据、附件导入和网页管理面板继续保留。旧 `workbench` 调用返回迁移提示，更新后请刷新客户端工具目录。

支持能力协商的客户端可使用 MCP 2026 Tasks 查询和取消原生 `exec`；未声明能力的客户端继续使用持久回执。取消确认不代表进程已停止，也不保证远端 SSH 副作用回滚。工具升级后需要刷新客户端目录。完整协议范围、参数和示例见 [MCP 工具参考](docs/CORE_TOOLS.md)。

### 在面板中使用 Pi、Codex 或 Claude Code

先在 Agent 所在电脑安装所需 CLI，完成其本机登录或模型配置，并确认使用 Agent 运行账号能够正常启动。CodePier 使用这些已有环境，不会把面板登录当成模型服务登录。

进入 **CLI 会话 → 新对话**，选择项目、CLI、模型和工作子目录，再发送第一条消息。会话在首次发送时创建；仅选择项目不会调用模型。

侧栏跨项目显示已同步会话，并保留项目归属、CLI 类型和运行状态。可以搜索和筛选历史，发送文件或图片，按 CLI 支持情况处理工具审批、交互提问、模型设置和中断。Claude Code 的差异见 [Claude 会话说明](docs/CLAUDE_CLI.md)。

Agent 离线时仍可阅读已同步历史，但不能继续本机执行。未发送草稿保存在当前页面内存中，刷新整个页面前应先处理；删除网页会话记录不会删除本机原生历史或项目源码。模型列表加载失败时，使用模型目录的刷新入口重试。详细行为见 [CLI 会话指南](docs/CLI_SESSIONS.md)。

### 通过项目使用 VPS

在 **VPS 管理 → 添加 VPS** 保存主机、端口、用户名和密码，再把连接分配给需要使用的项目。支持一台 VPS 对应多个项目，也支持一个项目使用多台 VPS。

配置完成后，可以这样发起只读检查：

> SSH 到 demo 项目分配的测试服务器，检查磁盘占用和服务状态，不要修改配置或重启服务。

客户端先使用 `vps` 查找连接，再将返回的 `target` 交给 `exec`。SSH 从项目所在的 Agent 发起，因此该电脑必须能连接目标服务器，并具备相应的 SSH 依赖和执行权限。

保存的密码不会返回给模型。默认严格检查 SSH 主机密钥，首次连接需要明确核实；不要通过关闭主机密钥校验来绕过报错。连接分配、凭据保存和多目标选择见 [VPS 指南](docs/VPS.md)。

### 验证代码、网页与桌面操作

**开发工具** 提供代码导航、工作目录、测试验收和任务交接。语言服务需要先在本机安装并配置；Git worktree 从明确提交创建，不复制原目录未提交的修改，也不会自动提交或合并。

验收记录保存命令、工作目录、退出码、输出和源码指纹。源码变化后，旧结果会失效或标记为过期，不能当作新代码已经通过测试。

后台浏览器使用本机扩展和原生消息宿主，需要分别授权项目、浏览器档案和网站 origin，并准备可用的后台标签页。它可以复用已授权档案的登录状态，不会把浏览器账号复制到 Hub。安装步骤见 [本机集成指南](docs/INTEGRATIONS-20260917.md)。

桌面控制是另一项独立能力，需要本机启用、项目允许、客户端 `computer` 授权和相应系统权限。各平台要求与紧急停止方法见 [Computer Use](docs/COMPUTER_USE.md)。工具出现在列表中，不代表目标电脑已经配置好这项能力。

### 查看调用详情

进入 **操作审计 → 工具执行**，按项目、工具、来源或状态筛选，展开一条记录即可查看保存的参数摘要、输出、结果和执行链路。

排查长时间无响应时，先看操作处于排队、等待资源还是实际执行阶段，再核对退出码。详情读取不会重新执行原工具；缺少的计时会显示“未记录”，不会推测模型的思考时间。

页面支持暂停实时更新、搜索和导出本页调用。参数与输出会做脱敏和截断，但不会保存完整文件正文供日志回放；分享日志前仍应检查敏感信息。详情见 [工具调用流水](docs/CALL_LOG.md)。

## 权限与数据

### 项目、账号和执行边界

CodePier 的权限分层生效：**用户与 Space 成员关系**决定资源归属和可见性，**Agent 本机配置**决定可用目录和能力，**项目映射**决定项目开放什么，**OAuth / PAT 的固定范围或动态角色**决定当前连接能做什么。浏览器和桌面还各有自己的本机授权。

ChatGPT 的项目名、对话名或工具参数不会切换凭据身份，也不会自动建立隔离。使用 `get_profile` 和 `get_access_context` 核对实际连接。MCP 默认减少账户名、绝对根路径等宿主元数据，但源码、命令输出和明确读取的资源仍可能包含这些信息。

**完整 Shell、构建脚本和原生 CLI 使用 Agent 运行账号的系统权限。** 项目目录、Space 和角色都不能替代 OS 沙箱；互不信任的用户应使用独立系统账号、容器或虚拟机。

使用固定范围连接、经常新增项目时，可在 **系统设置 → MCP 默认授权** 预选“全部现有及未来新增项目”。已有连接可以在 **MCP 接入 → 调整项目范围** 中修改；也可以明确确认，将全部项目范围应用到已有有效 OAuth 连接，无需为每个新项目重连。

这只调整项目范围，不增加已有凭据的工具权限，也不恢复已撤销授权。关闭默认选项只影响之后的预选；需要收回已有访问权时，应缩小对应连接的范围或撤销连接。

请只映射必要目录，使用可信 HTTPS，并妥善保管 Hub 数据、Agent 配置和模型凭据。Shell 使用系统账号权限；原生 CLI 也沿用自己的执行和审批策略。读取的源码、网页和工具输出都可能包含不可信内容，不能把其中的文字当成新的操作授权。完整说明见 [安全指南](SECURITY.md)。

### 多用户协作与外部 MCP

- **个人与团队 Space**：团队空间支持邀请成员、分配角色，明确共享工作流和交付物。共用角色不会公开个人会话历史；交互式 CLI、浏览器和桌面会话不会因此变成共享会话。
- **OIDC 登录**：支持授权码/PKCE、显式账号关联、经验证的群组映射和撤权。群组授权以 UserInfo 为来源，需核对提供者兼容性，并保留本地恢复管理员。
- **动态角色**：成员获准委派后，可以用自己的 Access Profile 建立角色连接。明确同意后，后续请求按当前角色规则检查；旧 fixed grant 保持原授权语义，不会自动迁移。
- **MCP 网关**：支持多个 HTTP MCP 后端、私有或明确共享的后端账号、已审核工具与角色规则。只有明确同意外部委派的连接才会获得对应工具。网关默认启用不等于已连接任何后端。

外部工具能访问什么，还取决于后端凭据。CodePier 不为任意外部服务提供通用项目沙箱，也不把不确定的外部调用自动重放。当前网关未实现后端 OAuth 登录刷新、stdio 进程托管或 MCP Apps 代理，详见[网关指南](docs/MCP_GATEWAY.md)。

团队配置、分享 API 和迁移步骤见[多用户/OIDC](docs/MULTIUSER_OIDC.md)与[动态角色](docs/DYNAMIC_ROLES.md)。

## 更新与备份

从 1.15 之前的版本升级时，先备份数据库及配套密钥并阅读[迁移与恢复指南](docs/MULTIUSER_OIDC.md)。旧版本回退需要匹配的源码和备份，不能让旧程序直接打开已迁移的新数据库。

### 更新面板

在 **系统设置 → 面板更新** 检查 GitHub 正式 Release，再确认更新。更新器校验源码包、构建候选镜像、复制数据卷并检查新服务；切换失败时按所处阶段回退或保留恢复状态。

该入口需要先在 Linux/systemd、Docker Compose 宿主机启用独立更新服务。部署目录和宿主机 Python 必须符合 root 所有权及不可被其他用户写入的要求；在准备好的部署目录执行：

```bash
sudo bash install.sh --enable-panel-update
```

新版就绪后页面会自动刷新；有未保存表单、草稿或进行中的操作时会暂缓。**面板更新会更新服务器提供的 Agent 安装文件，不会强制升级或重启各台电脑上的 Agent。** 支持范围、手动部署注意事项和故障恢复见 [面板更新指南](docs/PANEL_UPDATE.md)。

### 更新或卸载 Agent

在目标电脑的本机终端，以原安装账号执行。macOS / Linux：

```bash
"$HOME/.codepier-agent/codepier-agent" status
"$HOME/.codepier-agent/codepier-agent" upgrade
```

Windows PowerShell：

```powershell
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" status
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" upgrade
```

确定卸载时，将对应命令末尾的 `upgrade` 换成 `uninstall`。也可以从设备节点的 **完整管理 → 命令升级 / 卸载** 生成命令。升级保留设备身份、配对和已有配置；卸载需要确认节点名称，删除本机受管安装，不删除安装目录外的项目源码或 Hub 上的记录。维护前先结束相关任务，不要通过正在维护的 Agent 自己执行卸载。详见 [Agent 生命周期](docs/AGENT_INSTALL.md)。

### 备份 Hub 数据

Hub CLI 可以一致性备份数据库及对应主密钥。在部署目录执行：

```bash
backup="codepier-$(date +%Y%m%d-%H%M%S).zip"
docker compose exec -T hub python -m hub backup --output "/app/data/backups/$backup"
umask 077
mkdir -p backups
chmod 700 backups
docker compose cp "hub:/app/data/backups/$backup" "./backups/$backup"
chmod 600 "./backups/$backup"
```

将备份放到另一处可信存储，并验证可恢复性。数据库与对应密钥必须一起保留；这个 ZIP 不包含全部附件、Agent 本机状态或原生 CLI 配置。完整迁移还需规划数据卷、部署配置和各节点的备份。面板更新保留的旧数据卷也不能替代独立备份。

## 常见问题

**节点在线，但命令仍提示没有权限。** 检查 Agent 的 Shell / 目录任务配置、项目执行设置和客户端 `execute` 权限。旧 Agent 升级会保留原先关闭的执行设置；面板的“已有 Agent 开启执行能力”提供对应本机命令。

**新增项目后，ChatGPT 看不到它。** 用 `get_access_context` 检查连接。固定范围连接需要调整项目范围；已同意未来项目范围或符合当前动态角色规则的新项目才会可见。工具目录变化需要另外刷新客户端元数据。

**请求超时，不知道有没有执行。** 保存并查询原 `operation_id`，到调用流水查看实际状态和输出。不要换一个幂等键重复执行。取消 SSH 的本机进程也不保证远端命令已经停止或回滚。

**浏览器或桌面工具存在，却无法使用。** 先查看能力状态。安装程序、项目权限、网站授权和本机系统权限分别检查；普通文件读写成功不能证明这些条件已满足。

**Agent 装在 Windows 上，关闭终端后会不会退出？** 受管安装使用后台计划任务；注册开机任务需要一次 UAC 授权，Agent 仍以原账号的普通权限运行。非交互登录不提供交互桌面，映射盘和依赖桌面登录的凭据需要另行配置。

更多连接、安装和恢复问题见 [FAQ](docs/FAQ.md)。

## 本地开发

使用 Python 3.13 和 Node.js 24。以下为 macOS / Linux 命令；Windows 的虚拟环境入口位于 `.venv\Scripts\`。请在完整源码目录执行：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt -r requirements-tools.txt
python3.13 -m venv .venv-compat
.venv-compat/bin/python -m pip install --require-hashes -r requirements-compat.txt
.venv/bin/python -m playwright install chromium webkit
npm --prefix web/mcp-apps ci --ignore-scripts
npm --prefix web/mcp-apps run build
.venv/bin/python scripts/build_integration_assets.py
.venv/bin/python scripts/build_web_assets.py
```

先做静态检查和冒烟测试：

```bash
.venv/bin/python -m ruff check agent hub shared scripts tests
.venv/bin/python -m pytest -q -m smoke
.venv/bin/python scripts/check_release.py
```

完整回归使用新的输出目录：

```bash
.venv/bin/python scripts/check_full_regression.py \
  --output .work/regression-local --workers 2
```

Linux 浏览器测试还需要相应系统依赖；MCP SDK 兼容测试使用独立环境。构建规则、测试分层和 CI 说明见 [开发指南](docs/DEVELOPMENT.md)。

```text
agent/       本机文件、命令、原生 CLI 和集成能力
hub/         Web API、MCP、认证、项目与持久状态
shared/      共用协议、配置、权限与工具
web/         管理面板、MCP Apps 和浏览器扩展
deploy/      安装脚本、容器和服务配置
scripts/     构建、验证、迁移与维护工具
skills/      项目内置工作流说明
tests/       单元、集成、浏览器与平台测试
docs/        使用和维护文档
```

欢迎提交 Issue 或 Pull Request。问题反馈请附上版本、操作系统、复现步骤和经过脱敏的错误输出，不要上传配对票据、配置密钥或完整数据库。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；安全问题按 [SECURITY.md](SECURITY.md) 中的方式报告。

## 文档

附件导入、来源策略和失败恢复见[文件导入指南](docs/FILE_IMPORT.md)。

| 要做的事情 | 文档 |
| --- | --- |
| 从零安装并完成第一次调用 | [入门教程](docs/START-HERE.md) · [ChatGPT 接入](docs/CHATGPT.md) |
| 安装、修复或迁移 Agent | [安装与维护](docs/AGENT_INSTALL.md) · [生命周期](docs/AGENT_LIFECYCLE.md) · [旧版命名迁移](docs/CODEPIER-MIGRATION.md) |
| 使用 MCP、工作台与长任务 | [当前工具参考](docs/CORE_TOOLS.md) · [持久操作](docs/LONG_OPERATIONS.md) |
| 配置团队、身份与其他 MCP 服务 | [多用户/OIDC](docs/MULTIUSER_OIDC.md) · [动态角色](docs/DYNAMIC_ROLES.md) · [MCP 网关](docs/MCP_GATEWAY.md) |
| 使用原生会话和远程服务器 | [CLI 会话](docs/CLI_SESSIONS.md) · [Claude Code](docs/CLAUDE_CLI.md) · [VPS](docs/VPS.md) |
| 配置网页验证和桌面控制 | [本机集成](docs/INTEGRATIONS-20260917.md) · [Computer Use](docs/COMPUTER_USE.md) |
| 更新、排错和检查记录 | [面板更新](docs/PANEL_UPDATE.md) · [调用流水](docs/CALL_LOG.md) · [FAQ](docs/FAQ.md) |
| 部署或参与开发 | [环境配置](.env.example) · [架构](docs/ARCHITECTURE.md) · [开发与验证](docs/DEVELOPMENT.md) · [发布流程](docs/RELEASING.md) |

## 版本与许可

当前仓库的 `RELEASE.json` 标记为 **1.17.0 / released / source-and-image**。1.17 统一管理页面、字段和手机全页编辑，补强工作区隐私与会话恢复；ChatGPT 旧工作台组件已退役，Web 远程工作台、只读查询、标准 Tasks 和改动审阅继续保留。详见 [CHANGELOG.md](CHANGELOG.md)。

`main` 可能包含正式发布后的改动。安装和更新请核对目标 [Release](https://github.com/cyeinfpro/codepier/releases)；GitHub 提交或 Release 发布不会自动升级现用 Hub 与 Agent。

- **Version:** 1.17.0
- **License:** [MIT](LICENSE)

第三方依赖保留各自许可证。前端依赖声明位于 `web/vendor/`，MCP Apps 的声明见 [THIRD_PARTY_NOTICES.txt](web/mcp-apps/THIRD_PARTY_NOTICES.txt)。
