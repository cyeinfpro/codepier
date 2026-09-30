# CodePier Agent 安装、升级与卸载

CodePier 1.9.0 的旧版命名迁移见 [完整迁移说明](CODEPIER-MIGRATION.md)。旧版更新后由独立助手在空闲时迁移默认目录和系统服务，不重新配对；旧目录只保留精确兼容入口。自定义项目、外部状态目录和原授权不改名或扩大范围。`agentctl` 是兼容命令，新入口为 `codepier-agent`。


以下说明对应本次更新后的源码。新入口需先随 Hub 源码部署；旧线上面板不会自动获得新的下载脚本。本次源码修改和测试不代表已经更新服务器或家里电脑的现用 Agent。

## 一键安装

面板 **设备节点 → 接入电脑**，填写设备名称、目标电脑能访问的面板地址、系统和一个明确的授权目录，生成安装命令后在目标电脑自己的终端执行。安装器准备独立 Python 3.13 环境、下载经 SHA-256 校验的 Agent 包、配对并配置自动启动。

安装命令包含约 15 分钟有效、成功兑换即失效的一次性票据。不要贴到聊天、公开仓库或日志中；过期时重新生成。票据和配对密钥不写入浏览器本地存储。

目标电脑需要能主动访问面板和依赖下载源，无需开放家里的入站端口。Linux/macOS 需要 Bash 和 curl；Windows 使用 PowerShell。授权目录必须已经存在并填写绝对路径，例如 `/Users/me/Projects`、`/home/me/Projects` 或 `D:\Projects`。HTTP 不保护浏览器密码和首次配对数据，请使用 HTTPS、可信网络或 SSH 转发。

默认安装目录是当前用户的 `.codepier-agent`，不是授权项目目录。请使用原安装账号；不要为解决错误随意添加 sudo 或改用管理员账号。安装完成后还需要在面板配置具体项目映射和权限。

Windows 默认配置为开机后台任务，**未登录桌面也启动，关闭安装窗口不会退出**。注册开机触发器需要接受 UAC；提权仅用于注册，Agent 保持原安装账户的普通权限。使用无窗口 `pythonw.exe`，每分钟补拉退出进程，事件循环卡死 120 秒后触发重启。网络断开持续重连，不触发卡死重启。升级和卸载期间暂停自动补拉。

Windows 后台任务采用 S4U 非交互登录，不保存 Windows 密码；它不提供交互桌面，也不支持依赖桌面登录的映射盘、Windows 集成网络认证或加密凭据。桌面控制和使用这些凭据的工具需要另外配置交互会话。

## 执行能力默认值与旧配置

当前源码的新安装弹窗默认勾选“安装时开启 Shell 与目录任务”，可以取消。生成的安装命令会把选择传递到本机初始化；取消后，Shell 和新目录的任务能力均保持关闭。源码初始化支持 `init --shell disabled`，POSIX 一键安装入口支持 `--shell disabled`，PowerShell 安装入口支持 `-Shell disabled`。

文件工具仍限制在授权目录；完整 Shell 使用安装账号自身的系统权限，不是目录沙箱。安装完成后，面板项目仍需允许执行，客户端仍需 `execute` 授权。桌面控制不会随此选项开启。

已有 Agent 的普通升级、修复和重新配对保留原来的执行设置，不会因新安装默认值变化而扩大权限。旧版本缺少该设置或已经明确关闭时，在 **系统设置 → 已有 Agent 开启执行能力** 查看适用于默认受管安装的本机命令；自定义安装使用原运行目录、原解释器和原配置，不要重新初始化。配置生效后再次验证并保存项目映射即可，无需重新配对。

只需要具名任务、不需要 Shell 时，可仅在原配置中为对应授权目录设置 `allow_tasks=true`。关闭 Shell 不会自动删除任务或关闭单独授权的目录任务。这些当前源码行为优先于历史版本验收文档中的旧默认值。

## 已安装不再阻止升级

重复执行**同一个面板节点**的新安装命令，会验证现有受管服务、准备并检查候选运行时，然后进入修复升级，而不是直接提示“已经安装”。保留授权目录、任务和其他配置；配对票据中的节点编号必须与本机一致。

不要为了升级另建节点，也不要删除 `config.json` 绕过检查。票据属于另一节点、安装目录不完整或服务不属于该目录时，安装器会停止。手工源码运行的 Agent 与受管安装不同，不能通过猜测目录强行卸载。

## 面板生成一键升级、卸载命令

在设备节点点击 **完整管理 → 命令升级 / 卸载**。选择目标系统；自定义安装时填写 Agent 的绝对安装目录，默认安装可留空。复制对应命令到目标电脑的本机终端执行。

这两类命令不需要安装票据，不重新配对、不轮换密钥，并检查命令所指的节点是否与本机一致。命令固定本次脚本及安装包校验值；面板后续更新导致旧校验值失效时重新生成，不要删掉校验参数。

不要通过正在维护的 Agent 自己的远程 Shell 执行这些命令。先结束本机任务、原生终端会话及其他生命周期操作，否则会被安全检查拒绝。

## 安装后可重复使用的本机命令

新版安装或升级会在安装目录生成管理入口。Linux/macOS：

```bash
# 从本机配置的 Hub 获取当前安装包，校验后升级
"$HOME/.codepier-agent/codepier-agent" upgrade

# 查看安装信息，不显示连接密钥
"$HOME/.codepier-agent/codepier-agent" status

# 本机卸载；终端需要输入完整节点名称确认
"$HOME/.codepier-agent/codepier-agent" uninstall
```

Windows PowerShell：

```powershell
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" upgrade
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" status
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" start
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" uninstall
```

自定义安装时将路径换成实际安装目录。旧 Agent 没有 `agentctl` 时，先在已部署新版的面板生成一次管理命令；也可以使用完整新版源码包里的入口：

```bash
bash deploy/install-from-hub.sh upgrade --install-dir "$HOME/.codepier-agent"
bash deploy/install-from-hub.sh uninstall --install-dir "$HOME/.codepier-agent"
```

升级需要访问本机配置中的 Hub；本机 `uninstall` 和 `status` 不要求面板在线，也不下载 Python 或安装包。已损坏的解释器、缺失的服务定义或非受管目录会明确报错，不会猜测并删除文件。

`upgrade` 保留设备身份、连接密钥、项目权限、任务和历史；普通升级只切换运行时。首次 CodePier 命名迁移另外调整受管目录路径字段，并保存配置原始字节备份，详见迁移说明。下载、校验或依赖安装失败不会覆盖旧运行时；新服务启动检查失败由生命周期助手回滚。上一运行时保留在 `.runtime-previous`。

## 卸载范围和确认

卸载删除本机 Agent 服务、运行时、配置、密钥、日志和位于安装目录中的状态。需要保留本机备份、检查点或日志时，请先单独备份。安装目录之外的项目文件，以及 Hub 的节点记录、项目映射和审计记录均不删除。

如果授权项目目录位于 Agent 安装目录内部，卸载会拒绝继续，避免连同项目一起删除。若配置了安装目录之外的自定义 `state_dir`，不会通过猜测路径递归删除该外部状态目录，请自行按备份策略管理。

默认必须准确输入节点名称。仅在已经确认卸载范围的自动化流程中显式使用跳过交互确认的参数：

```bash
"$HOME/.codepier-agent/codepier-agent" uninstall --yes
```

```powershell
& "$env:USERPROFILE\.codepier-agent\codepier-agent.ps1" uninstall -Yes
```

服务停止失败时保留文件，不再使用“忽略停止错误后直接 rm -rf”的卸载方式。Windows 在 Python 退出后由独立进程清理仍被占用的安装目录，PowerShell 入口检查目录确已移除才报告完成。实际卸载成功后的再次调用源码入口会提示没有安装、无需清理；已删除的 `agentctl` 文件本身当然不能再次执行。

卸载本机 Agent 不等于删除面板记录。需要删除面板记录时，在节点详情中另行操作。

## 安装完成但自启动失败

安装器保留已经配对的配置，并输出恢复命令。使用原安装账号执行，路径以实际输出为准：

```bash
"$HOME/.codepier-agent/runtime/.venv/bin/python" \
  "$HOME/.codepier-agent/runtime/scripts/install_agent.py" \
  --install-dir "$HOME/.codepier-agent" --start-service
```

```powershell
& "$env:USERPROFILE\.codepier-agent\runtime\.venv\Scripts\python.exe" `
  "$env:USERPROFILE\.codepier-agent\runtime\scripts\install_agent.py" `
  --install-dir "$env:USERPROFILE\.codepier-agent" --start-service
```

先确认没有另一个 Agent 实例。恢复仍失败时保留日志，检查目标账号、目录权限和 systemd/launchd/任务计划程序状态，不要删除密钥强行重装。

旧 Windows 受管安装在本机执行新版升级或修复命令后，会将登录触发改为开机触发；需要接受一次 UAC。面板远程更新会更新看门狗，但不能代替本机完成首次开机任务授权。源码安装则先关闭旧前台进程，再运行新版 `deploy\start-agent.cmd`，保留原配置并迁入受管运行时。默认日志目录为 `%USERPROFILE%\.codepier-agent\logs`。

Windows 原生验收可在管理员终端运行 `python scripts/check_windows_service.py --output dist/windows-service.json`。它只创建临时任务和模拟 Agent，验证启动命令退出后继续运行、进程被杀恢复、卡死恢复和维护期间不被补拉，最后删除临时任务。该测试不重启或注销电脑；实际重启且不登录时的节点在线状态仍需在目标机器验证。

## 验证边界

本轮在 MacStudio 的隔离测试目录执行 Python、Bash 和 Chromium 回归；真实系统服务调用通过测试替身隔离，没有停止或卸载现用 Agent。Windows PowerShell 分支有命令契约检查，但没有 Windows 实机安装、升级、卸载验收；Linux systemd 也没有本轮实机部署验收。完整记录见 [开发与验证](DEVELOPMENT.md)。
