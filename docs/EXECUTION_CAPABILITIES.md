# MCP 执行策略：模型/CLI 与原生桌面

此策略是静态调用防线，不是 OS 沙箱，也不能证明任意程序或 UI 的语义。它不改生产配置，不创建凭据，不启用桌面权限。

## 两个独立开关

- Hub：`MCP_BLOCK_LOCAL_CODEX` 阻止 MCP 发起的 Codex 模型/CLI 调用；`MCP_BLOCK_NATIVE_COMPUTER` 阻止原生 computer 调用。
- Agent：`mcp_policy.block_local_codex` 与 `mcp_policy.block_native_computer` 分别提供本机底线，必须为 JSON 布尔值。
- 新 computer 阻断键缺省为 false，与同层旧 Codex 键独立，不写回配置。升级支持 v2 的 Hub/Agent 后，已有 computer 授权与本机白名单的调用默认不再被模型/CLI 禁令连带拦截；明确设置的阻断 true 仍有效。
- Hub 原生开关的明确 false 值为 `0/false/no/off`（忽略大小写和两端空白）；其他显式值保守阻止，避免拼写错误放行。
- MCP 的最终阻止状态是 Hub 与 Agent 同一能力的 OR。另一能力的放行不能抵销该能力的阻止。

| 同层旧 Codex 键 | 新 computer 键 | 模型/CLI | 原生 computer |
| --- | --- | --- | --- |
| false/未配置 | 未配置 | 策略允许 | 策略允许 |
| true | 未配置 | 阻止 | 策略允许 |
| true | false | 阻止 | 策略允许 |
| false | true | 策略允许 | 阻止 |
| false | false | 策略允许 | 策略允许 |
| true | true | 阻止 | 阻止 |

“策略允许”不代表授权成立：computer scope、设备与项目状态、本机 computer.enabled/projects/allowed_apps、owner/root 绑定、TTL、最新观察、原生审批、撤权和紧急停止仍逐项检查。浏览器授权与浏览器禁令不受此变更影响。本机 computer.enabled 缺省时，仅已有非空 projects 和 allowed_apps 才默认 true；缺少任一白名单仍关闭，明确 false 保留。旧配置中写入的 false 不会被覆盖。不会添加项目、应用、computer scope 或 OS 权限，也不会自动部署或启动 provider。

## 可信元数据与滚动升级

当前传输策略版本为 2，包含 origin、block_local_codex、block_native_computer。Hub 在认证后生成，派发及持久恢复前重新计算；工具参数不能注入。新策略不写入原幂等请求指纹。未开始的排队操作按当前策略检查；已接受或结果不明的操作仍只查原回执，不自动重发输入。

新 Agent 只接受合法 v2 元数据提供原生能力。缺失、v1、未知版本、错误类型一律阻止需要原生 provider 的调用，不能借旧协议获得拆分放行。模型/CLI 的合法 v1 与旧本机底线保持兼容。旧 Agent 将 v2 视为未知策略并保守阻止它检查的 Codex/原生调用；升级期间可能需要同时升级 Hub 与 Agent。普通文件读取和普通命令不因此被全面关闭。

只有可信、格式正确的 panel 元数据保留既有面板策略豁免；这不是 computer scope、本机授权、owner/root 或原生审批的豁免。旧协议也不能靠 origin=panel 获得新的原生能力。

## provider 不等于模型 turn

原生 adapter 的 Codex App Server 在临时隔离 home 中启动，建立 ephemeral thread，仅通过允许的原生 MCP 方法调用 computer provider；不复制用户模型凭据，不发起模型 turn。允许这条内部受限 transport 不会允许 shell/task 中运行 codex、codex app-server、Codex SDK 或模型会话。直接执行这些命令仍走模型/CLI 静态检查。

模型/CLI 阻止时，已知 Codex app 名称、bundle ID、应用路径不能作为普通原生目标开会话；既有 Codex 目标的观察/动作也重新检查模型策略，关闭会话仍可用。应用别名、终端内拼接命令、浏览器内模型界面和任意 UI 语义不是静态检查能完整识别的范围：需要严格的应用白名单与独立操作审批，不能将此策略称为任意模型调用沙箱。

## 返回与诊断

- 模型/CLI 或已知 Codex UI 目标：`CODEX_REMOTE_DISABLED`。
- 原生能力策略阻止：`NATIVE_COMPUTER_REMOTE_DISABLED`。
- `computer_status(probe=false)` 不启动 provider，保留 read 权限的静态查看。probe=true 在准入、派发、回执与 Agent 都另需当前 computer scope，不能用只读凭据初始化 provider；关闭现有会话仍继续走 computer scope 与原 owner/root 检查。
- `execution_info.execution_policy` 分别显示本机和有效的 Codex/native computer 阻止状态，并声明支持的两个策略能力；这些字段不是实际桌面权限证明，`os_sandbox` 仍为 false。

本地 focused tests 使用纯策略真值表、临时配置、mock provider/原生调用陷阱；不启动真实桌面、不调用模型。完整 Hub/Agent、浏览器和真实桌面验收应分开报告。
