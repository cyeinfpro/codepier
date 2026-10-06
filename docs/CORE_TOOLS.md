# 九个 MCP 工具

CodePier 的九个核心开发工具为 `workspace`、`read`、`write`、`edit`、`exec`、`process`、`vps`、`browser`、`computer`。旧的独立 MCP 工具入口已移除，调用会返回 `TOOL_REMOVED`；`full`、`coding` 目录也不恢复旧入口。面板和 Agent 内部仍复用原有执行、权限和持久化实现。

身份工具 `get_profile`、`get_access_context` 额外提供稳定身份和实时权限摘要。经明确同意的动态角色连接还可使用已审核的外部 MCP 工具及 `gateway_call_get` 回执查询，详见 [MCP 网关](MCP_GATEWAY.md)。

## 工作区与宿主信息

原生 MCP 默认按“项目工作区、执行节点”返回信息。项目的 `root`、上下文的 `project_root` 和工作目录记录的 `path` 为 `.`，表示当前项目或 `workspace_id` 对应的目录，不能据此把不同项目或工作目录合并。节点 ID 保持可调用，显示名称变为稳定代号；节点名称、账户名、UID、系统类型、工具安装路径及凭据环境变量存在情况不再出现在默认执行概览中。`execution.tools` 的值为可用性布尔值；Shell 保留必要的程序名称、权限状态、超时限制和真实的非沙箱边界。

技能目录按 `skill_id` 发现；带 ID 的技能摘要 `path/resource_path` 为相对技能资源的 `SKILL.md`，应通过 `workspace(operation="skill", skill_id=..., resource_path="SKILL.md")` 读取，不能作为项目根目录下的文件读取。显式读取技能时仍返回执行脚本所需的真实资源位置。项目内不带 ID 的技能路径仍相对于项目。具名任务目录保留任务名、说明、工作子目录和可用性，省略启动命令及环境变量清单。

同一展示规则覆盖 `workspace`、`project_query`、`process/task_query` 轮询、`rd://projects` 资源及标准 Tasks 终态。工具结果的 `content` 和 `structuredContent` 使用同一份投影，不把完整宿主信息复制到 `_meta`。HTTP 面板、Agent 协议、审计、SHA、上下文指纹及持久回执仍使用真实数据；投影只作用于返回副本。错误元数据中的常见账户主目录前缀替换为 `[account-home]`，保留错误码和诊断后缀。

这是减少默认宿主元数据的展示约定，不提供匿名化或隔离保证。源码、文档预览、补丁、命令输出、调用者提供的参数、明确请求的技能资源、原生界面和外部 MCP 结果保留原义，仍可能显示环境信息。`exec` 使用执行账号权限；需要宿主隔离时，应实际部署专用容器或虚拟机。修改工具描述后需在客户端刷新工具目录；已有对话中的旧结果不会被改写。

## 只读查询

ChatGPT 内的项目选择与任务看板已移除。`workbench` 不再出现在工具目录，旧调用按既有迁移约定返回 `TOOL_REMOVED`，提示改用 `project_query`。旧工作区资源仅返回无脚本、无工具调用的退役说明，不再列入资源目录。网页管理面板、项目上下文、工作流、原操作及证据存储不受影响。

`project_query` 只允许 `list/open/help/tree/skills/skill/tasks/status/readiness/dashboard/workflow_list/workflow_get`；`open` 不允许捕获基线。项目和任务仍必须明确选择，所有调用复用实时授权。`task_query` 只允许 `list/get/wait/trace/diagnostics/activity`，读取原操作，不执行、取消或重跑。参数及返回结构与对应的 `workspace`、`process` 操作一致；任务查询指 CodePier 已有操作回执，并非 MCP 标准 Tasks 协议。

只读发现从 `project_query` 开始，目录和技能读取无需调用混合工具。公开回执的等待、补读和追踪继续指向 `task_query`，保留原操作编号；捕获基线仍使用 `workspace`，显式取消仍使用 `process`。这些路由不改变实际授权，也不能保证宿主不再出现取消或拒绝提示。

这两个专用入口标注为只读；混合读写的 `workspace/process/browser/computer` 保持保守的非只读注解。工具目录不注册 global/thread 工作台或自动展示模板。九个核心工具的 `outputSchema` 描述实际成功、错误、持久 pending 变体；错误/等待不是成功，仍须核对原操作编号、状态和退出码。

## 标准 Tasks

现代 `2026-07-28` 客户端在每次请求的 `_meta["io.modelcontextprotocol/clientCapabilities"].extensions` 中声明 `"io.modelcontextprotocol/tasks": {}` 后，原生 `exec` 的真实 pending 回执可返回扁平 `resultType: "task"`。它复用原操作与幂等键，不新建执行器；未声明能力的请求及 legacy 连接保持原有结果。声明能力不强制把已完成的调用变为异步。

使用 `tasks/get` 读取状态与最终工具结果，`tasks/cancel` 请求合作式取消；`Mcp-Name` 必须镜像 `params.taskId`。取消确认不等于进程已经停止，SSH 远端副作用仍不保证回滚。工具退出非零属于 `completed` 加 `result.isError=true`，不是 JSON-RPC 层的 `failed`。本版没有需要用户输入的任务；`tasks/update` 对未知 `inputResponses` 做经授权的空确认，不把输入当作批准。不实现旧草案的 `tasks/list` 或 `tasks/result`。

Task 绑定原 operation、Space、用户和确切创建 grant，每次查询/取消及 await 后重验权限；新连接不会因同属一个用户而继承旧任务。终态随 operation 更新在同一数据库事务内冻结，Agent 后续恢复不会让 Task 倒退或替换已冻结结果。任务记录沿用 Hub 数据库备份和原操作的生命周期，`ttlMs=null` 表示不设定时过期；它不是绕过撤权的长期访问授权。该追加表不改变 OAuth、PKCE、资源标识、角色或下游同意。

项目上下文继续通过 `project_query(operation="open")` 和 `workspace(operation="context")` 读取；文件内容使用带 SHA 的 `read`。原工作台专用的“选定上下文”按钮随看板移除，附件导入契约 `write(operation="import")` 保持不变。

参考 [Tasks 2026-07-28](https://github.com/modelcontextprotocol/ext-tasks/blob/main/specification/2026-07-28/tasks.md)。本阶段没有接入标准 Tasks 输入请求/MRTR、OpenAI扩展表单、文件编辑器或宿主文件写入。后续需要单独实现请求状态绑定、明确确认、字段能力协商、版本/etag冲突与相应真实宿主验收，不能把这些入口视为已实现。

## 请求关联与分阶段诊断

到达 Hub 的每个 `/mcp` HTTP 请求都会生成独立的服务端随机 ID，通过 `X-CodePier-Request-ID` 响应头返回；现代成功/工具错误结果同时在 `_meta["com.codepier/requestId"]` 返回。客户端自带 ID 不会覆盖它。legacy 结果正文保持不变。ID 只用于排障，不能代替授权、操作编号或幂等键，也不能据此重放结果不明的写入。

`codepier_mcp_request` 结构化日志覆盖入口、认证、解析、协议校验、授权路由、执行、返回和响应交接。日志只含服务端 ID、枚举阶段/状态、白名单协议方法、已解析且限长的工具标签、有效操作编号、耗时与错误码；不新增记录请求正文、参数、命令、令牌、Cookie、URL、身份信息或异常文本。原项目活动记录在现有权限过滤下提供同一 `request_id`；认证或格式检查前失败无需先创建活动记录。

诊断按进程全局令牌桶采样，持续最多5请求/秒、突发50请求，每请求最多12条事件，保留终结事件；超额数量每60秒汇总一次。采样不影响请求处理和响应 ID。日志缺失可能来自采样、代理或宿主拦截，不能单独证明用户取消。只有真正到达服务器的请求才能获得这个 ID；这项源码改动无法解释或修复宿主在发出请求前生成的“用户取消”提示。

传输断开/协程中断记为 `transport_interrupted`，不等于显式 `tasks/cancel`，不会触发业务取消或重跑；取消方法只记录经授权的确认，仍需查询原操作终态。生产部署后才能观测这些新字段，本地验证不能证明生产或所有 ChatGPT 客户端已使用它们。

## 常用流程

```json
{"name":"workspace","arguments":{"operation":"list"}}
{"name":"workspace","arguments":{"operation":"open","project":"Imago"}}
{"name":"read","arguments":{"project":"Imago","path":"README.md"}}
{"name":"exec","arguments":{"project":"Imago","command":"git status --short","yield_seconds":1,"idempotency_key":"status-intent-001"}}
{"name":"process","arguments":{"operation":"wait","operation_ids":["返回的操作编号"],"wait_seconds":5}}
```

文件路径相对于已授权项目。读取文本默认最多 2,000 行、50 KiB，按完整行截断，用 `next_offset` 继续并携带 `expected_sha256`；支持读取不超过 16 MiB 的文本文件。PNG/JPEG 返回原生 MCP 图片块，沿用现有图片大小和过期限制。其他二进制文件使用产物导出。

`write` 需要 `expected_sha256`，创建新文件用 `new`，自动创建父目录并保存备份。`edit` 的所有 `old_text` 都匹配同一份原文件；重复、缺失或重叠匹配不会修改文件。保留 UTF-8 BOM 和原文件换行。也可传入 `changes` 做带 SHA 检查的多文件写入、删除、移动，或用 `dry_run` 预览。

## 专项能力按需发现

先调用 `workspace(operation="help")` 获得操作索引，再调用例如 `workspace(operation="help", tool="process", action="validate")` 获得所需权限、必填项和准确参数 schema。专项参数放在 `options`，`project`、`workspace_id`、`idempotency_key` 放在顶层，不允许从 `options` 覆盖。常用操作的额外筛选、分页参数也可放在 `options`，仍按该操作的原始类型校验。宿主上传附件时使用 `write(operation="import", file=宿主文件对象, options={"path":"目标路径"})`。

| 工具 | 操作 |
| --- | --- |
| `workspace` | `devices/project_create/list/open/skills/skill/tasks/status/readiness/tree/help`；`resolve/context/dashboard`；`worktree_create/worktree_list/worktree_remove`；`workflow_create/workflow_list/workflow_get/workflow_update/handoff`；`lsp_status` |
| `read` | 默认 `file`；`changes/artifact/artifacts/history/symbols/lsp` |
| `write` | 默认 `file`；`import/artifact` |
| `edit` | 默认 `file`；`restore/checkpoint` |
| `exec` | 本机命令、已配置任务、保存的 VPS 命令 |
| `process` | `list/get/wait/cancel/trace/diagnostics/agent/activity`；`search_start/search_get/search_cancel`；`validate/validation_get/validation_list` |
| `vps` | 查询授权服务器，返回供 `exec` 使用的 `target` |
| `browser` | `status/open/snapshot/action/close` |
| `computer` | `status/apps/open/observe/action/close` |

这些操作保留原有约束：改动快照不可变、备份恢复需 SHA、产物需要授权并有有效期、工作目录有所有权、工作流证据需绑定真实操作、验证结果检查源码版本。LSP 可能启动本机进程，仍需要执行权限。独立的管理员控制/验收行为不开放给 MCP。

例：

```json
{"name":"process","arguments":{"operation":"validate","project":"Imago","options":{"command":"python -m pytest -q","label":"回归检查"},"idempotency_key":"validation-intent-001"}}
{"name":"read","arguments":{"operation":"changes","project":"Imago","options":{"baseline_ref":"打开工作区时取得的基线编号"}}}
```

## 原生附件导入

`write(operation="import")` 的 `file` 必须放在顶层；`options` 只填写目标路径和可选源 SHA-256。来源校验在 Agent 执行，只更新 Hub 不会更新旧 Agent 的允许列表。配置扩展、即时失败恢复、脱敏错误以及实际往返验收要求见[文件导入指南](FILE_IMPORT.md)。

## 并行与重叠操作

新 `exec` 的独立命令共用最多 4 个命令执行槽，文件操作使用另一个 4 槽池。一个长命令不会占住整个项目；读文件也不等待命令槽。文件操作自动按真实路径协调，目录与其子路径视为重叠，跨嵌套项目映射仍会检查冲突。多文件编辑一次取得整组资源，避免互相持有部分锁。

命令可声明 `resources`：

```json
{"name":"exec","arguments":{"project":"Imago","command":"npm test","resources":[{"kind":"path","name":"src","mode":"read"},{"kind":"path","name":".cache","mode":"write"},{"kind":"service","name":"test-database","mode":"write"}],"idempotency_key":"test-intent-001"}}
```

相同路径的读取可共享，写入与重叠读取/写入互斥；服务按名称协调。相对本机路径基于命令 `cwd` 解析，VPS 资源按 SSH 主机与端口隔离。远程相对路径因无法预先确认登录目录，按服务器根目录保守协调。等待者按发生冲突的顺序进入，不影响不相关路径。

已配置任务沿用本机任务的工作目录、环境和超时；任务调用的相对资源名称以项目根目录为基准，可用绝对路径明确声明任务子目录中的资源。

任意脚本可能动态决定文件或远程副作用，不能可靠自动推断。未声明资源表示调用方认为任务独立；未知的修改脚本应声明路径 `.`、模式 `write`。这些锁协调 CodePier 的任务，不阻止外部编辑器、人工操作或其他 Agent 进程。

排队期限同时覆盖等待项目、资源和执行槽；到期返回 `QUEUE_EXPIRED` 并释放等待，不必等前一个命令结束。执行开始后，使用命令自己的 `timeout_seconds`。用 `process(operation="trace")` 或查询时传 `include_trace=true` 查看等待阶段及有权查看的阻塞操作。

`exec` 的 `yield_seconds` 默认 1 秒，超过后返回持久操作编号。`process` 支持一次并行查询/等待最多 16 个编号。断线后查询原编号或用原幂等键恢复，不能因为暂未收到结果就换新键重跑。取消本机 SSH 进程不保证远端命令停止或回滚。当前命令执行是非交互式，不提供 PTY/stdin 会话。

## VPS、浏览器、桌面

`vps(project="Imago")` 返回 `target="vps:<id>"`，传入 `exec(project="Imago", target="vps:<id>", command="df -h", idempotency_key="…")`。保存的密码只在已验证投递时解密，沿用项目绑定、连接版本、主机密钥检查和 Agent 本机 Shell 授权；密码不会返回给模型。临时 SSH 可以通过本机 `exec` 使用已安装的 SSH 工具，秘密放在环境变量而非命令文本。

浏览器使用本机配置的扩展、档案、空闲标签页池和站点白名单；桌面使用本机配置的原生提供方与允许的应用。工具出现在目录不代表本机已安装或授权。先调用 `status`，按真实配置结果处理。输入操作使用最新 `observation_id`，桌面还需要独立 `computer` scope；会话所有权、观察有效期、单次消费、截图过期与不确定输入不重放的规则不变。

## 设计来源

参考 [Pi 的工具实现](https://github.com/badlogic/pi-mono/tree/d5629e20489ccf770ed90b5a33941cb3b7ef24d0/packages/coding-agent/src/core/tools) 的少量通用原语、文本分页、输出截断、原文件多处编辑与按文件协调思路，以及 [pi-mcp](https://github.com/mofelee/pi-mcp/tree/ecf3000ea6979ec33383ddbcce45f69e8c57a70c) 的精简工具目录。实现继续使用 CodePier 的路径授权、SHA 校验、备份、加密传输和持久回执；没有引入 Pi 运行时依赖。
