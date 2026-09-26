# 从安装到第一次使用

本教程以一台运行 Hub 的服务器和一台保存项目的开发电脑为例，完成安装、项目映射和第一次 MCP 调用。服务器与开发电脑可以是同一台机器，但 Hub 和 Agent 仍是两个独立组件。

已有安装请直接阅读 [Agent 维护](AGENT_INSTALL.md) 和 [面板更新](PANEL_UPDATE.md)。不要为了升级重新初始化数据库、删除配对文件或创建一个同名新节点。

## 开始前准备什么

| 位置 | 需要准备 |
| --- | --- |
| Hub 服务器 | Git、Bash、Docker Engine、Docker Compose v2，以及 Python 3.9+。Compose 需支持 `up --wait`。 |
| 开发电脑 | macOS、Windows 或 Linux；一个已经存在的项目目录；能够访问 Hub 和安装依赖的网络。macOS / Linux 安装入口使用 Bash 和 curl，Windows 使用 PowerShell。 |
| 公开接入 | 一个指向 Hub 服务器的域名和可信 HTTPS。使用仓库的 Caddy 配置时，需要让服务器的 80、443 端口可达且未被其他服务占用。 |
| 可选开发工具 | 项目本身需要的 Git、编译器和测试环境；使用原生会话时，另行安装并登录 Pi、Codex 或 Claude Code。 |

Docker 镜像提供 Hub 的 Python 3.13 运行环境，Agent 安装器准备独立的 Python 3.13 环境。宿主机 Python 3.9+ 只是 Hub 安装器的要求，不适用于直接从源码运行应用。

下文使用 `hub.example.com` 和项目名 `demo` 作为示例。执行前替换域名、账号和路径，不要照搬其他人的配置或密钥。

## 一、安装 Hub

### 获取正式版本

在服务器上下载源码。以下示例固定到 `v1.14.3`，其他正式版本可从 [Releases](https://github.com/cyeinfpro/codepier/releases) 选择：

```bash
git clone --branch v1.14.3 --depth 1 https://github.com/cyeinfpro/codepier.git
cd codepier
```

也可以下载 `codepier-VERSION-source-full.zip` 并解压到独立目录。不要只下载 `install.sh`：安装器需要同一份源码中的部署脚本、运行代码和依赖锁文件。

下面的 HTTPS 和可信网络 HTTP 是两种配置方式，选择其中一种即可。

### 方式 A：公网服务器使用 HTTPS

先完成域名解析，并确认 80、443 端口没有被现有代理占用。仓库提供 [Caddy 配置](../deploy/Caddyfile) 和 [Compose HTTPS 覆盖文件](../deploy/compose.https.yml)。

仅在**全新安装、尚无 `.env` 文件**时，从模板创建配置：

```bash
(umask 077; cp -n .env.example .env)
```

用服务器上的编辑器打开 `.env`，修改已有的同名项，并添加 `COMPOSE_FILE`。保留其余配置，不要把下面片段直接覆盖到已有安装的完整 `.env` 上：

```dotenv
COMPOSE_FILE=compose.yml:deploy/compose.https.yml
HUB_BIND_ADDRESS=127.0.0.1
HUB_PORT=8765
HUB_PUBLIC_URL=https://hub.example.com
MCP_PUBLIC_URL=https://hub.example.com
MCP_DOMAIN=hub.example.com
TLS_EMAIL=you@example.com
```

这样公网入口由 Caddy 提供，Hub 的 HTTP 端口只绑定服务器回环地址。开发电脑也使用 HTTPS 域名连接，不使用服务器的 `127.0.0.1`。

这里的 `COMPOSE_FILE` 分隔符适用于 Linux / macOS。将它保存在 `.env`，以后运行安装和维护命令时才能继续使用同一套 HTTPS 配置。不要将 `FORWARDED_ALLOW_IPS` 改成 `*`。

确认配置可解析，再运行安装器：

```bash
docker compose config --quiet
bash install.sh
```

安装器构建镜像、准备数据卷，并在终端询问管理员账号和密码。密码至少 12 位，没有预设账号密码可供直接登录。`.env` 已存在时，安装器沿用其中的地址设置。

服务器已经运行 Nginx、Caddy 或其他可信反向代理时，可以沿用现有代理，不必再启动第二个 Caddy。需要正确转发 HTTPS、WebSocket 和流式响应，设置实际公开地址，并只信任真实代理来源；具体配置以现有网络拓扑为准。

### 方式 B：在可信网络中先试用

在尚未创建 `.env` 的新源码目录执行：

```bash
bash install.sh
```

按提示填写开发电脑能访问的服务器 IP 或主机名、端口和管理员账号。这个入口默认生成 HTTP 配置，适用于受控内网或经过保护的连接，**不要通过裸公网 HTTP 输入管理员密码或配对设备**。

HTTP 试用不等于已经能够从 ChatGPT 公开连接。之后应配置 HTTPS，或者使用 [Secure MCP Tunnel 接入路线](CHATGPT.md)。

### 确认 Hub 正常运行

在部署目录执行，修改过端口时相应替换 `8765`：

```bash
docker compose ps
curl --fail http://127.0.0.1:8765/healthz
docker compose logs --tail=100 hub
```

HTTPS 部署还应从浏览器打开实际域名，确认没有证书错误，并使用刚设置的管理员账号登录。不要在安装尚未完成时，通过重新初始化数据库来处理登录失败。

CodePier 使用外部命名数据卷。首次安装应由 `install.sh` 准备数据，不要跳过安装器直接执行 `docker compose up`，也不要用删除数据卷的方式重试。

### 无人值守安装

自动化安装可以使用私密密码文件，避免将密码写进命令文本或 Shell 历史。先用本机编辑器创建文件并限制读取权限，再执行：

```bash
bash install.sh \
  --host hub.example.com \
  --port 8765 \
  --username admin \
  --password-file /secure/codepier-admin-password \
  --non-interactive
```

`/secure/codepier-admin-password` 必须替换为真实、可读且妥善保管的文件路径。首次没有 `.env` 时，`--host` 生成的是 HTTP 地址，不会自动配置 HTTPS；公网部署先准备上面的 HTTPS `.env`。已有 `.env` 时，地址以文件为准。安装完成后按自己的凭据保管策略处理密码文件。

## 二、准备一个测试项目

第一次接入建议使用不含敏感内容的小目录，先确认链路，再映射正式项目。在**开发电脑**上创建 `codepier-demo` 目录，并放入一个 `README.md`。

macOS / Linux 示例；目标目录已存在时会停止，不覆盖其中的文件：

```bash
mkdir -p "$HOME/Projects"
mkdir "$HOME/Projects/codepier-demo" && \
  printf '# CodePier demo\n\nA small project for checking the connection.\n' \
  > "$HOME/Projects/codepier-demo/README.md"
```

Windows 可以在资源管理器中创建 `D:\Projects\codepier-demo`，再新建同名 Markdown 文件；没有 D 盘时改用实际存在的磁盘。文件内容：

```markdown
# CodePier demo

A small project for checking the connection.
```

这个目录不需要初始化 Git，也不需要安装模型 CLI。后续映射的名称设为 `demo`，它只是面板中便于识别的项目名。

## 三、安装并配对 Agent

在面板打开 **设备节点 → 接入电脑**，填写设备名称、目标系统、开发电脑能访问的 Hub 地址，以及刚创建的目录的绝对路径。

例如，macOS 可以授权 `/Users/me/Projects/codepier-demo`，Linux 可以授权 `/home/me/Projects/codepier-demo`，Windows 可以授权 `D:\Projects\codepier-demo`。这里的 `me` 要替换成实际用户名；表单中填写绝对路径，不填 `~`。

新安装默认勾选 **Shell 与目录任务**。只想先验证文件访问，可以取消；浏览器和桌面控制不随这个选项自动开启。之后映射正式项目时，可以按实际需要在本机扩充授权目录，而不是一开始就授权整个磁盘。

生成命令后，在目标电脑的本机终端运行：macOS / Linux 使用终端，Windows 使用 PowerShell。不要在 Hub 服务器上运行另一台开发电脑的安装命令。

安装器会下载并校验 Agent、准备运行环境、配对并配置自动启动。一次性票据约 15 分钟有效，成功兑换后失效；过期后重新生成即可，不要把命令贴进公开 Issue 或截图。

安装完成后，回到面板确认设备在线。默认安装目录是当前用户的 `.codepier-agent`，与项目目录分开。以原安装账号运行，遇到权限错误时不要随意改用 root 或重新配对。

Windows 注册开机后台任务需要一次 UAC 确认，Agent 仍使用原安装账号的普通权限。后台任务不提供交互桌面，也不能假定能使用桌面登录后的映射盘或网络凭据。各系统恢复方法见 [Agent 安装指南](AGENT_INSTALL.md)。

## 四、创建项目映射

进入 **项目映射**，添加项目并填写：

| 字段 | 示例 |
| --- | --- |
| 项目名称 | `demo` |
| 所属设备 | 刚安装 Agent 的开发电脑 |
| 实际目录 | 第二步创建的 `codepier-demo` 绝对路径 |
| 初始权限 | 读取 |

保存后，在面板工作台选中 `demo`，打开 `README.md`。应能看到刚才写入的内容。这一步成功，才说明项目目录、设备连接和文件读取权限都已对上。

目录不被允许时，检查路径是否位于 Agent 本机授权范围内。面板项目设置不能自行扩大 Agent 的本机目录范围。

需要继续验证修改和命令时，再为项目开启写入与执行。命令能否运行，还取决于 Agent 本机是否启用了 Shell / 目录任务，以及客户端是否获得 `execute` 授权。

## 五、连接 MCP 客户端

### ChatGPT 公开连接

先在 **系统设置 → 公开地址** 中保存实际 HTTPS 基地址，例如 `https://hub.example.com`，不要加 `/mcp`。面板已保存的公开地址优先于环境变量，更换域名后需要一起检查。

在 ChatGPT 的 **Settings → Security and login** 中开启 **Developer mode**，进入 **Plugins**，通过加号创建连接。填写连接名称、说明和完整 MCP URL：

```text
https://hub.example.com/mcp
```

使用 OAuth 登录 CodePier，并核对授权的项目和工具范围。准备完成后面的全部练习时，授权 `demo` 的读取、写入和执行；只做只读试用时，仅选择读取即可。无论授予哪种权限，第一次调用都先从读取测试文件开始。

创建完成后核对发现的工具，再在新对话中启用 CodePier。客户端入口和可用权限受账号及工作区策略影响，以 [OpenAI 接入说明](https://developers.openai.com/plugins/deploy/connect-chatgpt) 为准。认证和元数据刷新细节见 [ChatGPT 接入指南](CHATGPT.md)。

### 私有 Hub 或其他客户端

不公开 Hub 时，可采用 **Secure MCP Tunnel + CodePier stdio 桥接器**。Tunnel 进程负责出站连接，桥接器使用 CodePier PAT 访问 Hub。Tunnel 运行凭据、CodePier PAT、管理员密码和 Agent 配对密钥用途不同，不要混用。完整配置见 [Tunnel 路线](CHATGPT.md)。

支持 HTTP Bearer 认证的 MCP 客户端，可以在 **MCP 接入** 创建限定项目及权限的 PAT，再按客户端的认证配置使用。令牌只显示一次，请放在客户端的私密配置中，不写入项目文件。

只在网页里使用 Pi、Codex 或 Claude 会话，不需要完成这一节的 MCP 接入。

## 六、完成第一次读、写和执行

### 先做只读检查

在启用 CodePier 的对话中发送：

> 打开 demo 项目，读取 README.md，告诉我文件内容和当前可以使用的权限。不要修改文件，也不要运行命令。

应能读到测试项目的内容。回到面板的 **操作审计 → 工具执行**，检查记录的项目、设备和路径是否正确。

### 创建一个测试文件

确认项目和客户端均已允许写入后，继续发送：

> 在 demo 项目中新建 codepier-check.txt，写入“CodePier 文件写入测试”。文件已存在时不要覆盖。完成后重新读取，核对保存的内容。

文件工具会在创建时使用 `expected_sha256="new"`，已有文件则需要先读取并使用对应 SHA 检查冲突。成功后，到开发电脑上确认文件确实出现在预期目录。

后续修改现有文件时，可以这样要求：

> 读取刚创建的 codepier-check.txt，在末尾增加一行“第二次检查”，然后展示差异。发现文件被其他人修改时先停止，不要覆盖。

### 运行一个无副作用的命令

在已安装 Git，并且本机、项目和客户端都允许执行的前提下发送：

> 在 demo 项目目录运行 git --version，报告实际输出和退出码，不要安装或更新任何程序。

这条命令不要求测试目录是 Git 仓库。如果找不到 Git，应报告真实错误，再由你决定是否配置该工具，不应把失败当成 CodePier 已完成验证。

实际项目中再替换为自己的测试或构建命令。耗时任务会返回 `operation_id`；记录显示排队时，等待的是原操作，不要重新提交同一任务。连接中断后也是查询原编号，不能仅凭客户端超时判断远端没有执行。

完成这三项后，你已经验证了文件读取、带冲突保护的写入和本机命令执行。删除测试文件或映射正式项目前，仍应确认操作范围。

## 七、按需要启用其他功能

**原生 CLI 会话：** 在 Agent 所在电脑安装并完成 Pi、Codex 或 Claude Code 的认证，然后在 **CLI 会话 → 新对话** 选择项目和 CLI。首条消息发送时才创建会话。模型配置、历史恢复和草稿边界见 [CLI 会话](CLI_SESSIONS.md)；Claude 的能力差异见 [Claude Code](CLAUDE_CLI.md)。

**VPS：** 在 **VPS 管理** 保存连接并分配给 `demo` 或正式项目。调用时由项目所在 Agent 发起 SSH，不是由 Hub 代连；先确认该电脑的网络和主机密钥。详见 [VPS 指南](VPS.md)。

**网页与桌面：** 后台浏览器需要安装扩展、原生消息宿主，并授权准确的浏览器档案和网站 origin；桌面控制需要独立的本机及客户端授权。详见 [本机集成](INTEGRATIONS-20260917.md) 和 [Computer Use](COMPUTER_USE.md)。

**持续项目授权：** 经常添加项目时，可在 **系统设置 → MCP 默认授权** 预选“全部现有及未来新增项目”。已有 OAuth / PAT 可在 **MCP 接入 → 调整项目范围** 修改，原凭据继续使用。调整项目范围不增加已有工具权限，也不等于刷新客户端工具目录。

## 排错时先检查哪一层

| 现象 | 先检查 |
| --- | --- |
| 面板打不开 | 容器状态、监听地址、端口、防火墙和反向代理。HTTPS 还要检查域名和证书。 |
| Agent 离线 | 开发电脑是否能访问所填 Hub 地址、本机服务是否运行，以及 Agent 日志。不要先删除配对配置。 |
| 设备在线但项目读不到 | 实际目录是否存在，是否位于 Agent 授权范围，是否选中了正确设备和项目。 |
| 能读文件但不能执行 | Agent 本机能力、项目执行设置、客户端 `execute` 权限，以及可执行程序是否在 Agent 环境中可见。 |
| 新项目未出现在客户端 | 当前授权是限定项目还是包含未来项目；按需调整范围。 |
| 请求一直等待或已超时 | 查询原操作编号，在调用详情中区分排队、等待资源、执行和已结束状态。 |
| CLI 模型目录加载失败 | 目标电脑上的 CLI 是否能以 Agent 账号启动；使用目录刷新重试，不要重复发送消息。 |

更多错误与恢复步骤见 [FAQ](FAQ.md) 和 [调用流水](CALL_LOG.md)。对外反馈问题时只分享经过检查和脱敏的版本、错误信息、操作编号及复现步骤。

## 后续维护

Hub 与 Agent 分别升级。面板的一键更新需要先配置宿主机更新服务，它更新服务器提供的 Agent 文件，不会强制重启开发电脑。执行 Agent 的升级或卸载命令时，应在本机以原安装账号操作，并先结束相关任务。

备份时同时保留 Hub 数据库和对应主密钥；数据库备份 ZIP 不能替代整个数据卷、Agent 状态和原生 CLI 配置的备份。操作命令见 [README 的更新与备份](../README.md#更新与备份)，详细维护见 [面板更新](PANEL_UPDATE.md)、[Agent 安装与维护](AGENT_INSTALL.md) 和 [安全指南](../SECURITY.md)。
