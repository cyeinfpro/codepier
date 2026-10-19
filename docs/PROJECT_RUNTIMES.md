# 项目运行时与连续执行

版本：1.30.0；更新：2026-10-19。本文描述源码能力；实际节点是否启用，以已认证握手、当前配置和原操作回执为准。

## 面板设置（2026-10-17）

入口：**项目映射 → 对应项目卡片 → 执行环境**。页面提供清洁、兼容和容器三种方式，以及可选的 Shell、逐行 PATH、环境变量名称与容器资源参数。清洁模式默认复用已有 GitHub CLI、Git 配置和 SSH Agent，可按项目分别关闭，无需输入或复制令牌。

当前源码的实际默认值已调整为 `project_clean_v1`：没有独立运行配置的新旧项目都使用清洁模式；明确配置的 `native_compat` 或 `container_v1` 保持不变。界面不是单独预选一个按钮；默认规则由 Agent 的 `default_profile()` 与执行选路共同决定。清洁模式默认查找项目 `.venv`、`node_modules/.bin` 和按平台确定的工具目录，不继承个人登录 PATH。

点击模式只修改当前草稿，点击“保存并应用”才提交。后台通过面板管理员会话、CSRF、项目稳定 ID 与节点管理权限进行校验，用私有的类型化 Agent 操作保存指定项目配置；MCP 工具目录不开放这些配置写入能力。不会借普通 Shell 改整份配置，也不会改变授权根、账号密钥或 Shell 执行开关。切换为清洁模式不代表获得 OS 隔离。

页面区分“已保存 · 待生效”和“已生效”。配置保存后保留备份，Agent 沿原配置监听机制加载；短暂重连期间继续核对原配置，不重复提交。只有重新读取到节点实际加载的运行时修订后才显示已生效。当前配置变化不会重启已经运行的命令；下一条新命令使用加载后的配置。严格任务仍保留原隔离要求，不能通过面板选择其他模式绕过。

刷新页面后的恢复只在当前账号、Space 与项目下保留原保存编号，最多 16 条、24 小时；不存配置、路径、环境值或令牌。恢复历史成功后仍重新读取节点；发生后续配置变化时展示最新配置而不覆盖。未知写入可通过“只读核对当前配置”排障，不转成自动重发。后台只读核对有退避与次数上限，页面不可见时暂停。

并发窗口以配置修订号进行冲突检查。冲突时保留草稿并要求重新读取；断线时保留原保存编号，通过只读回执接口核对，不用新编号重放。取消、关闭或切换项目不会提交草稿；关闭设置页也不会取消已提交的后台保存。窗口关闭后的迟到响应不会重新打开旧页面。

节点离线或旧 Agent 不支持此契约时禁用保存，实际模式显示未知；推荐默认值不冒充节点当前状态。容器模式需要固定镜像摘要、已安装的 Docker 程序和可用本地 socket；缺少依赖时不保存、不自动拉镜像、不降级为原生执行。受保护环境变量值不返回面板，相同模式的原有值由节点侧保留。

上述为源码实现。部署仍需更新匹配的 Hub 和 Agent、刷新页面资源；源码、配置已保存、节点已加载和真实运行验收是不同状态。

## 1. 选择执行方式

| 配置 | 行为 | 真实边界 |
| --- | --- | --- |
| `native_compat` | 明确选择时沿用原 Shell、原环境继承和原任务语义 | 原执行账号权限，不是目录沙箱 |
| `project_clean_v1` | 默认模式；非登录 Shell、独立 HOME/tmp/cache、明确 PATH；具名任务直接执行 argv | 环境可预测，但仍是原执行账号权限 |
| `container_v1` | 固定镜像、受限 Linux 容器、共享指定项目目录；启动前核验实际配置 | 每次操作单独证明配置边界，不是匿名环境或虚拟机保证 |

不同模式不增加任何 read/write/execute/computer scope，不修改已有授权、房间范围、租约或 checkpoint。源码与日志仍可能出现系统信息；这种信息不能自行成为桌面控制的前置条件，也不能解除真实平台拒绝。

## 2. 项目级配置

配置放在受保护的 Agent 配置中，不从项目 README、仓库 JSON 或模型参数加载。精确项目 ID 优先使用；别名也支持，但两者同时配置且不一致时失败关闭。不能使用通配符。

示例配置片段：

```json
{
  "project_runtimes": {
    "PROJECT_ID_OR_EXACT_ALIAS": {
      "schema_version": 1,
      "mode": "project_clean_v1",
      "shell": "sh",
      "path": [".venv/bin", "node_modules/.bin"],
      "pass_env": ["HTTPS_PROXY", "SSL_CERT_FILE"],
      "env": {"PROJECT_ENV": "development"}
    }
  }
}
```

PATH 的相对项以实际工作区根目录解析，随后补充系统默认路径。不要把冒号分隔的整段 PATH 填成一个目录。Python 虚拟环境的可执行文件保留其调用路径，不把 venv 符号链接强制解析成基础解释器。Node/nvm、Homebrew、证书、代理和签名工具需要按项目明确验证；不会复制整个个人环境来凑兼容。

清洁模式的 Shell 可选 `sh`、`bash`、`powershell`。sh 使用 `/bin/sh -c`；bash 关闭 profile/rc；PowerShell 使用 NoProfile/NonInteractive。未启用清洁模式的项目保留原 Shell 选择。独立 HOME 是执行配置，不改变实际 UID 或操作系统权限。

HOME、USER、PATH、SHELL、ENV/BASH_ENV、TMP/XDG 等运行时保留字段不能由 pass_env、profile.env 或单次调用覆盖。允许变量的显式空字符串仍保留，不解释为删除；Windows 按环境变量名大小写不敏感的规则保护保留字段。必要代理/证书等变量通过明确的 pass_env 或 env 配置引入；不要把完整父环境或宿主凭据目录暴露给容器。就绪查询只观察依赖，不创建私有目录，也不代表项目任务已经通过。

### 2.1 预览与应用

使用现有受信任 Python，在代码根目录执行：

```sh
python -m agent.runtime_config doctor \
  --config /protected/agent/config.json \
  --project PROJECT_ID --root /approved/project

python -m agent.runtime_config configure \
  --config /protected/agent/config.json \
  --project PROJECT_ID --root /approved/project \
  --profile-json /protected/project-profile.json
```

`doctor` 与未带 `--apply` 的 configure 只读：不会创建 HOME、启动项目程序、拉镜像或修改授权。预览输出当前 config_sha256 和配置就绪状态，不输出凭据值和个人路径。

明确应用使用刚读到的完整 SHA：

```sh
python -m agent.runtime_config configure \
  --config /protected/agent/config.json \
  --project PROJECT_ID --root /approved/project \
  --profile-json /protected/project-profile.json \
  --apply --expected-sha256 CURRENT_CONFIG_SHA256
```

配置读取拒绝重复 JSON 键、超出 1 MiB 的配置、FIFO 和读取期间发生替换的文件。新写锁只有在验证原进程已退出后才恢复，旧版本或归属未知的锁不会被强制移除。备份与配置文件先刷新，POSIX 上同时确认父目录落盘；替换后落盘不确定返回 DURABILITY_UNCONFIRMED，不会谎报未写入。

应用保留原始配置的其他字段，包括身份、密钥、roots、任务、Shell 开关。使用当前 SHA、文件身份复核、独占协作写锁、0600 备份和原子替换；冲突不会猜测覆盖。写锁是合作式并发约定，不能代替操作系统访问权限。不要同时从其他配置编辑器写同一文件。

启用后沿用已有 Agent 配置重新加载机制；核对新的原生握手与 project_query(open/status) 中的 runtime 修订，不把配置写入成功当作 Agent 已加载。回退为 native_compat 需要再次明确修改配置，不能用失败后的自动回退绕开原隔离要求。进行中的原操作和历史回执保持不变。

### 2.2 清洁模式复用已有开发授权

清洁模式默认复用 Agent 执行账号已有的 GitHub CLI、Git 全局配置和 SSH Agent 来源，使新旧项目在独立 HOME 下仍能继续使用有效的原登录。无需逐项目开启；需要调整时，在 **执行环境 → 复用已有登录** 中更改选项，沿原“保存并应用”完成。项目已经显式关闭的选项会继续关闭。

| 选项 | 接入方式 | 保留的行为 |
| --- | --- | --- |
| GitHub CLI | 原非空 GH_CONFIG_DIR 优先；否则按原 XDG_CONFIG_HOME、Windows AppData、用户目录定位 gh 配置；Windows 用户目录取 USERPROFILE，其他平台取 HOME；可继承已有 GitHub / Enterprise token 环境变量 | 由 gh 沿用已有配置和操作系统凭据存储；不读取并复制 hosts.yml 中的令牌 |
| Git 配置 | 原 GIT_CONFIG_GLOBAL 优先；否则用私有引用文件按 XDG git/config → HOME/.gitconfig 顺序引入 | 保持 global 层次，仓库和工作区配置仍优先；不把用户配置内容复制进项目源码 |
| SSH Agent | 传递原 SSH_AUTH_SOCK | 继续由原 SSH Agent 提供签名；不导出私钥、不执行 ssh-add、不创建新部署密钥 |

三项默认开启。省略整个 credentials 字段、使用空对象 {}，或只设置其中一项时，所有缺省项均按 true 处理；显式 false 关闭对应的自动复用。默认规则在读取时生效，不要求重写已有项目配置。下列片段显式写出默认值，保留原配置中的 shell、path 等工具设置；项目选择通过受保护的面板配置保存：

```json
{
  "mode": "project_clean_v1",
  "credentials": {
    "github_cli": true,
    "git_config": true,
    "ssh_agent": true
  }
}
```

例如，`"credentials": {"ssh_agent": false}` 仅关闭 SSH Agent 自动复用，GitHub CLI 与 Git 配置仍沿用默认值。null、字符串、数字及未知选项均被拒绝；native_compat 和 container_v1 的既有规则保持不变。

这些开关不扩大 CodePier 的项目、工具、VPS 或桌面权限；启用后，项目内已获准的命令可使用所选账号原本具备的权限。项目隔离与第三方账号的仓库授权是不同边界，仍应使用适合该项目的账号或令牌。运行时继续如实返回 project_clean_v1 / isolation=none，不把环境整理描述成 OS 沙箱。

项目原有 pass_env、profile.env 和单次允许的环境设置优先，显式空字符串保持不变。显式 GITHUB_TOKEN 不会被自动继承的高优先级 GH_TOKEN 遮盖；显式 GH_CONFIG_DIR 不会被自动附加的原账号 token 遮盖。已明确提供项目 token 时，也不自动补入原账号的 GH_HOST。原 GH_CONFIG_DIR 或 GIT_CONFIG_GLOBAL 明确指向缺失位置时保留该选择，不静默切到另一份账号配置；自动复用的这两个原生路径必须为绝对路径，避免更换工作目录后选中另一份配置。

原进程 GH_CONFIG_DIR 为空时，按 gh 语义继续寻找原账号的默认目录；原进程 GIT_CONFIG_GLOBAL 为空则按 Git 语义保留空值、不回落到默认全局配置。复用来源属于 Agent 的执行账号；另一个系统账号的登录不会被扫描或自动接入。

SSH 和 GitHub 凭据不写入源码、面板回执或状态报告。经环境变量注入的 GitHub / Enterprise token 与 SSHPASS 会在进程输出流和持久日志前按实际值脱敏；这不代替对任意文件内容、编码后的秘密或第三方工具输出的访问控制。配置摘要继续使用带密钥的修订值，所选 token 或 socket 变量改变后会产生不同修订，不公开其值。

关闭复用后，新命令不再自动设置对应来源。Git 引用文件位于私有 HOME 中的独立文件，未启用时不会被 Git 的默认配置查找自动读取。已运行进程、已发送请求和已存在的手动 pass_env/env 配置保持各自原有语义；关闭自动复用不撤销第三方账号本身的登录。

面板与 doctor 只返回每类来源的 enabled、state 和 source：disabled 表示未启用自动复用，missing 表示未发现该来源，configured 表示来源已配置。**configured 不表示已经通过 GitHub API、Git 远端或 SSH 服务器认证**。状态查询不创建私有目录、不发起登录或网络认证；凭据是否过期、账号是否有目标仓库权限，由实际工具调用确认。

适用边界：

- GitHub CLI 配置复用与 Git HTTPS 凭据配置分别生效；使用 gh 作为 Git helper 的项目通常需要同时启用前两项。
- Git 的相对 include 仍由 Git 相对于原配置文件解析；配置内显式使用 ~/、依赖个人 HOME 的自定义 helper、credential-store 默认路径或发布脚本手写的 ~/.ssh/key，需要改为项目明确配置的路径或原有 SSH Agent。不会把整个用户 HOME、.ssh 目录或浏览器登录目录映射进清洁环境。
- SSH Agent 状态仅说明 socket 来源已设置，不证明 Agent 已解锁或载入了需要的密钥；已有 VPS 目标仍沿原独立授权和传输链路执行。
- container_v1 和 native_compat 拒绝 credentials 字段。容器仍不接入原账号凭据或管理 socket；兼容模式继续使用原环境继承规则。
- 新设置需要匹配的 Hub 与 Agent 私有面板契约。旧节点通过契约协商禁用设置，不能显示“已生效”却忽略凭据选择。

环境变量语义依据：[GitHub CLI 环境变量](https://cli.github.com/manual/gh_help_environment)、[Git 全局配置变量](https://git-scm.com/docs/git)、[Git 配置层次与 include](https://git-scm.com/docs/git-config)、[OpenSSH 环境变量](https://man.openbsd.org/ssh.1)。

## 3. 常规开发使用项目能力

open 返回 `capability_index`，按本次调用的有效权限提供搜索、changes、validation、tasks、artifacts 的现有帮助入口。tools/list、聚合与单项 help、输入 schema 和结果中的下一步提示使用同一发现策略；权限变化后重新计算，不以最近打开的项目改变连接范围。每次实际调用仍独立校验当前权限。

只读调用可发现 browser/computer 的静态 status。computer.status 的 probe=false 使用 read，probe=true 另需 computer；桌面动作继续要求 computer。`read.lsp` 会运行已配置的语言服务，仍要求 execute，只读账号的目录和帮助不提供此操作。旧客户端直接调用隐藏入口也不会绕过后端鉴权。

需要严格参数时使用返回的 project_query(help) 请求。配置好的任务继续通过 `exec(task=...)` 执行；其 command 为受保护 Agent 配置内的 argv 数组，不经过额外登录 Shell。需要管道或重定向时继续使用 exec(command=...)，由项目指定的 Shell 解析。

默认开发索引不枚举设备/桌面管理。原工具目录仍由既有授权和配置决定，不能仅因调用了 open 就偷偷改变同一连接的工具范围。原生 browser/computer、VPS 和其他管理入口保持自己的真实授权与能力边界。清洁模式不接受直接原生 SSH 调用，但明确选择的已保存 VPS 是独立授权目标：沿原绑定和凭据运行原生 SSH 传输，不把它冒充本地清洁运行时，也不从失败的本地命令自动切换。容器模式和原任务的严格隔离要求仍禁止原生 SSH，不会降级。

## 4. 结果、日志和恢复

命令结果可增加：

- `runtime`：项目绑定的 opaque runtime_ref、revision、模式与真实边界。
- `continuation`：原 project/workspace/operation、runtime_ref/revision；不是授权令牌。
- `failure_context`：原代码、退出码、超时/取消/未知结果分类；不解析日志中的审批 JSON。
- `log_ref`：指回原操作的已授权输出；派生 diagnostics 标记 untrusted。
- 容器操作附带 `execution_isolation`、`runtime_outcome`、`runtime_cleanup`。

`runtime_outcome` 区分 Docker 客户端退出和实际工作负载退出；只有真实容器终态观察才能证明工作负载结果。创建/启动/清理结果不明确时保留 needs_review，不能用客户端退出 0 报告完成。清理失败也不能算完整执行成功。

原始 stdout/stderr、文件正文、diff、SHA、截断和输出序列均保留真实语义，已有 SecretOutput 凭据保护不被移除。派生相对路径摘要不是原始日志。直接结果、轮询和标准 Tasks 均复用同一展示白名单；服务端持久回执保留原数据。

可跳转诊断只使用可信的项目根、执行目录及实际容器映射。项目外路径、相似前缀、没有可靠参照目录的相对路径不会被猜成项目文件；命令内部切换目录后也不沿用启动目录作假定位。无法解析的内容保留在原始输出中，普通短输出无需再取一次日志。展示投影不改写原回执，也不改变执行配置修订。

测试包装器支持显式项目视图：

```sh
python scripts/managed_test_run.py \
  --output-root .codepier-test-runs --cwd . --result-view project \
  --project PROJECT_ID --timeout 900 \
  -- python -m pytest -p no:cacheprovider
```

仅包装器自己生成的 evidence/manifest 元数据变为项目相对路径；原 manifest 和日志不改写。项目外的证据明确标记 outside_project，不伪造成项目内路径。原 absolute 模式仍兼容旧调用者。

Dot 每次读取原连接契约时会得到轻量 runtime_contract。原任务恢复仍按原 operation_id/checkpoint/lease/fence，不新建任务、不重复执行副作用。日志中出现系统词不是授权拒绝；真实 CodePier/平台拒绝依然有效。调用平台没有可信反馈时保持 not_observed，不虚构已批准或未批准。

验证结果除源码指纹外还记录 runtime revision。配置改变后，旧验证仍是历史事实，但不能继续当作当前环境通过；需要重新执行实际验证。

## 5. 容器执行配置与验收

容器配置示例仅展示字段，镜像摘要必须替换为已批准、已安装的真实镜像：

```json
{
  "mode": "container_v1",
  "shell": "sh",
  "path": ["/usr/local/bin", "/usr/bin", "/bin"],
  "container": {
    "engine": "/absolute/path/to/docker",
    "endpoint": "unix:///absolute/path/to/docker.sock",
    "image": "sha256:REPLACE_WITH_64_HEX_DIGEST",
    "uid": 1000,
    "gid": 1000,
    "memory_mib": 1024,
    "cpus": 1,
    "pids_limit": 128,
    "tmp_mib": 256,
    "home_mib": 256
  }
}
```

要求 Linux Docker engine、可核验的默认 seccomp、受信任的固定镜像与本地 Unix socket。驱动不自动拉镜像，不接受任意 Docker 参数、远程 daemon、特权容器、宿主网络/PID/IPC、设备透传或额外挂载。

实际创建强制：非 root、只读根、cap-drop ALL、no-new-privileges、网络 none、私有 IPC/cgroup、进程/内存/CPU 限额；只有指定项目可写挂载到 /workspace，HOME/tmp 使用受限 tmpfs。创建后检查实际 inspect 字段和镜像/命令/挂载，再启动项目程序。缺少任何必要条件即拒绝，不退回原生 Shell。

挂载前拒绝根目录、个人 HOME/其祖先、明显系统目录、外部 Git worktree 元数据、嵌套文件系统、socket/FIFO/设备和多硬链接文件；扫描预算不足时拒绝。扫描不是冻结快照：执行节点、Docker daemon 与项目映射由受信任所有者控制，不支持恶意宿主并发重写挂载对象的威胁模型。需要更强租户边界时应采用独立 VM/独立工作区，而不是把本驱动夸大成物理宿主隔离证明。

所有文件工具和容器命令使用同一份批准的项目目录。容器写入就是该项目的实际写入，不是只读预览或自动回滚。外部 gitdir、平台原生签名/Xcode/桌面流程和依赖联网安装不能假定可无差别迁移。网络 none 下需要已具备依赖的镜像/项目。

严格房间模式 `require_os_isolation` 在原 owner 范围操作中绑定经认证 Agent 广告的具体配置修订；这只是“必须使用这个驱动”的要求，不是已经通过 OS 验证。原请求持久化该要求，在发送前和 Agent 启动前再次核验。旧 Agent、配置改变、跨目标或隔离证据不足都失败关闭。模型不能通过 isolated=true、日志或新的 task ID 取得权限。

### 5.1 真实容器验收入口

准备受保护的合法 container_v1 profile 文件，其固定镜像必须具备 sh、Python 3 和 sleep，然后运行：

```sh
python scripts/verify_project_runtime.py \
  --profile-json /protected/container-profile.json \
  --archive /verified/codepier-1.30.0-source-full.zip \
  --commit EXACT_40_HEX_COMMIT \
  --output /evidence/runtime-acceptance.json
```

从被核验的最终公开归档解包后的源码运行资格脚本，脚本同时核对实际执行源码与归档的逐文件内容、权限和清单。传入一个提交号或归档路径本身不能证明源码一致。

该资格验证在受控临时项目中实际检查非 root、指定工作区、双向共享读写、网络限制、宿主绝对路径和相邻项目不可达、跨界链接和管理 socket 不可用、原操作幂等及重复查询、真实非零退出、超时与主动取消、容器清理。根外 canary 由受信夹具创建，不能使用用户真实的其他项目或凭据。必需检查以 `shared/runtime_qualification.py` 的 `CHECKS` 为唯一清单，证据消费者拒绝缺项、额外项和非布尔真值。

缺少配置、服务、镜像或任一必需证据即失败，不能 skip 后宣布隔离通过。CI 的 Linux 公开归档构建必须运行此入口；正常的跨平台回归保留模拟拒绝测试，不把它们当作实机证明。发布证明绑定最终提交、归档 SHA 与源码清单，并保留实际运行结果和清理状态。

## 6. 上线检查

先核对目标代码、Hub/Agent 构建来源，再升级受支持实例；不要用版本字符串或 prepared 标记代替实际部署证明。首次仅选择明确项目做清洁模式兼容验证，确认工具链、任务、恢复、原始日志和配置回退，再逐项目迁移。

不静默替换所有项目配置，不重新创建用户连接/订阅，也不清空旧会话。客户端目录需实际刷新验证；真实 Dot 的新旧长会话与平台审批仍属于客户端验收，不能由 loopback 自动化推定完成。

公开发布证据与升级顺序见[发布流程](RELEASING.md)。本地调试报告与测试日志不随公开源码分发。
