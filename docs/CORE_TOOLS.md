# 九个 MCP 工具

CodePier 对外只提供 `workspace`、`read`、`write`、`edit`、`exec`、`process`、`vps`、`browser`、`computer`。旧的独立 MCP 工具入口已移除，调用会返回 `TOOL_REMOVED`；`full`、`coding` 目录也不恢复旧入口。面板和 Agent 内部仍复用原有执行、权限和持久化实现。

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
| `workspace` | `list/open/skills/skill/tasks/status/readiness/tree/help`；`resolve/context/dashboard`；`worktree_create/worktree_list/worktree_remove`；`workflow_create/workflow_list/workflow_get/workflow_update/handoff`；`lsp_status` |
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
