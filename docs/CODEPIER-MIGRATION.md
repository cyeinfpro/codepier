# CodePier 1.9.0 · 命名与旧版更新迁移

**CodePier · 码头 — AI 与本地代码对接、任务停靠的地方。**

这份说明对应源码更新，不是现用服务的部署记录。先更新 Hub，再通过更新后的面板更新 Agent。原节点、原授权和原连接地址继续使用，不需要为改名重新建立节点。

## 当前名称

| 范围 | CodePier 名称 |
| --- | --- |
| 产品与页面 | CodePier · 码头 |
| 标准源码目录 | `codepier`，服务器示例 `/opt/codepier` |
| 源码管理入口 | `./codepier`；Windows `codepier.ps1` |
| Agent 默认目录 | `$HOME/.codepier-agent`；Windows `%USERPROFILE%\.codepier-agent` |
| Agent 本机管理入口 | `codepier-agent` / `codepier-agent.ps1` |
| macOS launchd 服务 | `com.codepier.agent` |
| Linux systemd 服务 | `codepier-agent.service` |
| Windows 计划任务 | `CodePierAgent` |
| Docker 项目与镜像 | `codepier` / `codepier:1.9.0` |
| Hub、HTTPS 数据卷 | `codepier-hub-data`、`codepier-caddy-data`、`codepier-caddy-config` |
| 浏览器原生宿主 | `com.codepier.browser`；可执行文件 `codepier-browser-host` / `.exe` |
| 本机控制模块 | `agent.codepier_control`、`agent.codepier_browser_host` |
| 新项目卡片资源 | `ui://codepier/workspace-v1.html`、`ui://codepier/changes-v1.html` |
| 源码安装包 | `codepier-1.9.0-source.zip` |

MCP、Hub、Agent、Python、Codex、Pi、Docker 等协议或组件名称不是旧产品名，不进行错误替换。第三方依赖、版权声明和真实历史报告也不伪改。

## Hub：在原安装位置执行更新入口

把完整新版源码放到原安装位置，保留 `.env`。执行：

```bash
bash install.sh
```

HTTPS 安装必须沿用原来的 Compose 文件组合，例如：

```bash
COMPOSE_FILE=compose.yml:deploy/compose.https.yml bash install.sh
```

需要 Docker Engine、可用的 Compose 构建/启动组件，以及主机 Python 3.9 以上版本。主机 Python 只运行标准库迁移器，不需要安装项目依赖。可以通过 `CODEPIER_BOOTSTRAP_PYTHON` 指定它。首次无人值守安装继续支持 `--host`、`--port`、`--username`、`--password-file` 和 `--non-interactive`。`CODEPIER_ADMIN_PASSWORD` 优先，原 `RD_ADMIN_PASSWORD` 仍作为兼容输入；密码不写入进程参数或 `.env`。

安装器先验证配置并构建新镜像，旧服务在构建阶段继续运行。确认原 Hub、数据卷、代理服务和其他卷没有归属冲突后，才停止旧容器，记录它们原来的重启策略，复制原数据到独立备份卷和新卷。原卷只读，文件逐一校验 SHA-256，SQLite 检查完整性，WAL 中尚未合并进主文件的记录也保留。服务用户 UID/GID 仍为 10001，避免改显示用户名后丢失数据访问权限。

显式外部卷避免直接 `docker compose up` 在旧数据之外悄悄创建一个空面板。旧 HTTPS 代理使用固定子网时，只有确认属于原 Compose 项目、没有其他容器占用的桥接网络才会被记录、断开并移除；失败恢复保留原 IP、别名和网络设置。不会删除其他项目网络、修改主机防火墙或更换域名与证书。

主机 Nginx 等代理通过 Docker 网关访问 Hub 时，安装器会在停机前记录原 Hub 所连接、归属已验证的 Compose 桥接网络。新网络创建后、公开 Hub 启动前，只把 `FORWARDED_ALLOW_IPS` 中逐项明确配置的旧网关 IP 替换为对应新网关 IP，原子更新 `.env` 并重新解析 Compose 确认生效。其他受信任地址及 Caddy 固定代理 IP 保留，不自动扩大为子网或 `*`。迁移计划保存在上述事务记录中，重试不依赖已移除的旧网络。

若 `FORWARDED_ALLOW_IPS` 来自 Shell 导出变量、复杂 `.env` 表达式或 Compose 覆盖文件中的硬编码，无法安全迁移时安装器会报错；请在配置来源处明确设置地址后重试。不会通过禁用同源校验来绕过配置问题。验收反向代理部署时，除 `/healthz` 外，还应验证带登录态、CSRF 和正确 `Origin` 的 HTTPS POST 成功，以及错误 `Origin` 仍返回 403。

新服务尚未接触新数据时，失败会恢复原容器。新 Hub 可能已经写入新记录后，不会自动用旧数据库回滚；原卷、新卷和备份都保留，记录 `recovery_required`，再次执行安装入口时核验并继续使用新数据。只有新 Hub 的健康检查通过才报告完成。

当前安装目录中的 `.codepier-hub-upgrade.json` 记录阶段、卷名、备份和恢复信息。遇到未知或不完整状态，不要删除这个文件、执行 `down -v` 或清空数据卷。尚未跨过新数据写入边界的中断，可由本机管理员检查记录后执行：

```bash
python3 scripts/migrate_hub.py rollback --root "$PWD"
```

这个命令会拒绝把可能已有新数据的状态回滚到旧数据库。自定义 bind mount、未知网络驱动、另一服务占用或新旧双安装冲突会明确停止，不能靠猜测归属搬动数据。

## Agent：旧更新器也能完成第一次迁移

更新 Hub 后，在原节点点击“更新 Agent”，或在目标电脑自己的终端运行安装目录内的管理命令。旧目录仍存在时，新的安装/维护脚本会先识别它，不会先创建第二套新目录。

```bash
"$HOME/.codepier-agent/codepier-agent" upgrade
"$HOME/.codepier-agent/codepier-agent" status
```

Windows：

```powershell
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" upgrade
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" status
```

`agentctl` / `agentctl.ps1` 继续作为兼容命令。升级前应结束正在运行的任务、原生会话和桌面控制会话；自动命名迁移不会为改名强制取消它们。不要通过正在被维护的 Agent 自己的远程 Shell 执行卸载或本机维护。

新版 Agent 包保持旧更新器认可的结构。原更新器先完成候选运行时校验、切换、旧服务健康检查和回执。新版 Agent 启动后，等待原更新器完成并释放安装锁，再在无活动工作时把改名交给独立系统任务。Windows 的迁移 Python 也准备在被移动的目录之外，避免自身 DLL/解释器锁住安装目录。

独立助手检查服务定义归属、目标目录和新服务名是否被占用，备份原配置、管理记录、服务定义及一致性状态数据库，然后迁移默认目录。只改 `state_dir`、受管解释器/工具路径、生成的虚拟环境启动入口和服务定义中确实指向旧安装的路径；设备编号、密钥、Hub URL、项目授权、任务和用户自定义字段保持原值。新服务验证成功后才注销旧主服务并标记命名迁移完成。

第一次默认目录迁移会改变配置中必要的路径字段及 JSON 排版，因此不能声称整个配置文件仍逐字节相同；原始字节保存在迁移备份里。后续不涉及目录变化的运行时升级继续保留原配置。

备份与进度位于安装目录中的：

```text
backups/codepier-migration/<时间与唯一编号>/
.codepier-migration.json
management.json
```

新服务失败时恢复原配置、路径、运行环境入口和服务。若自动恢复本身也失败，明确标记 `recovery_required`，保留文件，不创建空的新目录冒充成功。应在原安装账号下检查记录、服务状态和备份；不要删除配置、换节点配对、添加 sudo 或盲目删除锁来跳过检查。运行时已升级、节点在线、命名迁移已完成是三个独立状态，面板分别显示。

## 浏览器、命令与历史兼容

面板新页面在读取外观设置之前迁移旧 `relay-*` 的外观与未完成操作回执。先写入新键并核对再删除旧键；冲突时优先保留新值，同时保留旧回执供核查；存储不可用不会导致页面启动失败。扩展保留原档案编号、站点授权、工作标签页、租约和防重放记录，不自动扩大浏览器权限。

原生宿主迁移只操作安装回执中哈希一致的文件及原用户注册项。自定义外部状态目录原地保留；其中确属本安装的桥接入口可按回执更新。旧宿主名保留为旧扩展的兼容入口，新宿主使用 CodePier 名称；卸载同时核验并移除这些属于本安装的入口，不删除浏览器档案、登录数据或项目文件。

旧 MCP 卡片 URI、绑定键、stdio 环境变量、Pi 图片占位符及模块命令仍可解析，新生成内容使用 CodePier。加密握手、现有凭据、Cookie 和已有协议资源中的兼容标识不是可随意替换的产品文案；改掉它们会破坏混合版本连接。已经发送到 ChatGPT 的旧消息是历史内容，不会被服务器重写；更新后重新打开页面/卡片使用新资源。

## 源码目录与不应强制改动的内容

标准旧源码目录 `remote-dev-mcp` 可通过安装入口或下列预览/应用命令迁移：

```bash
python3 scripts/rename_checkout.py
python3 scripts/rename_checkout.py --apply
```

只有确认是本项目的标准旧目录才会改成同级 `codepier`。原路径保留为精确兼容链接/Windows junction，已有项目映射、旧命令和外部引用仍能找到同一份文件，不是复制出第二套源码。已知生成的虚拟环境入口会更新，用户笔记和代码中的字符串不做全盘替换；有中断记录时可以验证后继续完成。

当前连接的 Home 名称和面板项目别名（例如 MCP）是用户已保存的连接/权限映射，不是软件产品名，保留为兼容入口。现有域名、授权范围、设备名称、自定义安装目录、外部状态目录、业务项目及已有 Git 隔离目录不因更名被强行移动。手工源码运行、不属于一键安装器管理的系统服务不会被猜测接管。

实际测试编号、源码校验、真实平台与未实测范围以 本地历史验收记录；当前验证方法见 [开发与验证](DEVELOPMENT.md) 为准。
