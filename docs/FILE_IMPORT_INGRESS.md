# 统一文件入站：协议、限制与部署验收

本页描述可选的新入站实现。源码和隔离测试通过不代表已部署，也不代表四种宿主都完成真实往返。所有新入口默认关闭；现有原生导入在未启用中转时仍使用 Agent 直取路径。

## 目标与路由

文件按字节透明传输，不因 JPEG、PDF、ZIP、视频、文档或未知扩展名采用不同权限。支持空文件和 Unicode 文件名；不解压、不执行、不自动赋予执行权限，不覆盖已存在目标。受保护路径、设备文件、链接及不安全路径仍拒绝。“全部文件”不表示无限大小或可以导入凭据。

两条适配路由共享同一个入站状态机：

1. 原生附件宿主提供顶层 `openai/fileParams` 文件对象。Hub 在显式来源策略内下载到有界临时文件，计算完整大小和 SHA-256，再走认证分块通道。宿主的 `file_id` 只是引用，不是来源证明。
2. 普通 MCP 客户端通过显式安装/配置的本地 stdio 适配器读取已批准根目录内的文件，以同一已有 Hub 身份发送字节。绝对源路径只在本地使用；HTTP 元数据只包含目标项目、相对路径、大小和摘要。

不复用 CLI 的执行权限或私有附件票据，不要求新增专用凭据服务，不允许调用方自行指定 owner。内部 `incoming_upload_*` 不是模型可直接调用的公开 MCP 工具。

## 认证 HTTP 协议

| 请求 | 含义 |
| --- | --- |
| `POST /api/file-imports` | JSON：project、workspace_id（可选）、path、size、sha256、idempotency_key |
| `GET /api/file-imports/{upload_id}` | 读取新鲜状态和已持久接收的偏移；可用原状态操作键恢复不确定回执 |
| `PUT /api/file-imports/{upload_id}/chunks?offset=N` | 原始二进制，最多 256 KiB，头 `X-Chunk-Sha256` |
| `POST /api/file-imports/{upload_id}/finish` | 空请求体；校验完整文件并无覆盖发布 |

每一步复用既有认证，重新检查当前 grant、用户、空间、项目、设备、项目根及 workspace 绑定。上传编号不是 bearer capability，不能跨用户、跨项目或换 grant 使用。接收 HTTP 请求体前后、远端操作准入和回执返回前都会检查授权；Agent 也复核绑定及根目录身份。

入站 manifest 只持久化绑定和进度元数据，不另建完整文件仓库；既有 Runtime 队列仍会持久保存尚未完成分块的加密 payload，终态后按原操作生命周期处理。数据库备份及 WAL 不能当成纯元数据。分块通过既有加密 Agent 通道发送，审计摘要不保存原始字节/base64、签名 URL 或源文件路径。Agent 在私有暂存目录持久化分块，核对重传内容、偏移和完整摘要。临时文件不会作为成功文件可见。

## 开关与来源策略

| 位置 | 设置 | 默认 |
| --- | --- | --- |
| Hub | `CODEPIER_FILE_IMPORT_STREAMING=true` | 关闭认证入站 |
| Hub | `CODEPIER_NATIVE_FILE_RELAY=true` | 关闭原生附件中转；还需上一个开关 |
| Hub | `CODEPIER_NATIVE_FILE_HOSTS` | 不设置则使用内置精确域名；JSON 数组显式替换基础列表 |
| Hub | `CODEPIER_NATIVE_FILE_PROVIDERS` | 命名来源 JSON 数组，默认空 |
| Agent | `integrations.file_import_streaming=true` | 不设置/false 为关闭 |
| Agent 直取 | `integrations.file_source_providers` | 命名来源数组，默认空 |
| 本地桥接 | `CODEPIER_FILE_IMPORT_ROOTS` | 绝对目录 JSON 数组；不设置则不展示本地上传工具 |

唯一可选命名来源 `openai_sediment` 接受 `sdmntpr[a-z0-9]{1,56}.oaiusercontent.com` 形式的单一 ASCII 子域。未知 provider、其他子域、多层域、Azure/AWS 任意账户和通配符全部拒绝。它必须由所有者审查并明确启用，不能根据失败主机自动启用。

精确主机和 provider 都只决定域名资格。HTTPS、443、无 userinfo、正常 TLS 证书、完整 DNS 公网单播校验、已核验 IP 连接、每跳重定向重验、限制长度和时间继续执行。不会发送浏览器 cookie 或额外 bearer 凭据，不用环境代理。中转启用后其失败不会自动降级到 Agent 直取；更换通道不能绕过来源拒绝。

`write(operation="source_check")` 接收相同顶层原生 file，返回当前来源策略及需要审查的精确主机。它是只读预检，不发下载请求，也不代表 DNS、TLS、内容或真实宿主往返已经通过。当前没有原生批量预检事务；批量调用方应为各文件保留稳定键和单独回执，遇到来源拒绝暂停。

## 大小、磁盘与清理

- Agent 单文件默认 128 MiB，现有 `max_import_bytes` 可限制到 1 字节至 512 MiB。0 字节文件仍可导入。
- 认证 HTTP 入口上限 512 MiB，实际还受目标 Agent 上限约束。原生 Hub 中转本阶段固定 128 MiB，最多 4 个同时下载缓冲，最多约 512 MiB 临时文件。
- Agent 预留预算为 2 GiB；每个上传按两倍声明大小预留，覆盖私有源暂存和目标同盘发布副本。最多 32 个同一绑定上传、256 个节点上传。
- Hub 最多 8 个同一身份活动上传、256 个全局活动上传；每身份每小时 60 次 begin；元数据总量有上限。HTTP 与远端并发也有界。
- 原生中转在下载前按 `(space_id, user_id)` 持久限额：每小时最多 60 个新 key、最多 64 个尚未关联入站 manifest 的 reservation、最多 512 条保留 identity（包含已完成记录），全局最多 4096 条。切换 grant 不重置同一所有者额度。新请求的明确来源拒绝、声明超限、节点离线或不支持能力不留下 identity；相同 key 和已完成恢复不消耗新额度，旧 URL 失效不阻止合法完成恢复。identity 至少保留 7 天，关联 manifest 仍存在时继续保留；达到上限只拒绝新 key，不删除有效回执或原始绑定。旧三列表仅从对应 manifest 或原 key 与 identity 均匹配的授权恢复回填所有者；无法归属的旧行仍保留并计入全局上限。
- 未完成上传 TTL 为 24 小时；成功回执保留 7 天。Agent 启动时发现旧上传表便运行清理，即使新上传开关已经关闭；后台每 5 分钟清理已登记且身份匹配的过期暂存。
- Hub 原生缓冲关闭后清理，不提供节点离线时“已持久接受文件”的保证。节点离线或不支持能力时，在下载前拒绝。
- 模糊发布状态保留证据和配额，要求原节点人工核查，不能为回收空间自动删除未确认的项目目标。操作系统磁盘满会给出脱敏存储错误，不能报告成功。

本地桥接只读取批准根内的普通单链接文件，以目录及文件描述符固定来源；保护规则覆盖整条祖先路径、凭据文件和桥接 token 文件。当前本地适配器仅支持 POSIX，Windows 明确拒绝。Agent 的 Windows 路径实现存在，但本阶段不能以 macOS 测试代替 Windows 实机验收。

## 重试、断连和发布边界

begin 的稳定键绑定用户/空间/目标/大小/摘要。同键改内容或改目标会冲突。分块稳定键绑定上传编号、偏移和摘要；重复确认返回原结果，不重复写入。接收偏移只有落盘后才推进。

传输异常先恢复原操作与状态，不生成新键来猜测成功。本地适配器有界重试临时网络错误，明确权限/来源拒绝不自动重试或扩权。原生中转重试在重新访问短效 URL 前查已完成的原始 finish 回执，重新授权、脱敏并验证大小/摘要/路径；因此“文件已到达但回复丢失”不需要重复发布。

未完成的原生中转可能需要宿主刷新同一文件的下载 URL，重新下载 Hub 临时缓冲后从 Agent 已接收偏移继续。原生请求优先绑定调用方提供的 expected_sha256；未提供时绑定 file_id 的摘要。宿主如果同时更换 file_id，不能自动认定为原文件；须恢复原回执或以已知摘要明确绑定，不能用新键猜测重传。Hub 重启不会保留匿名下载缓冲。状态不确定时要求读取原回执，不宣称文件未落盘。

发布前完整 SHA-256 校验，目标目录锚定且禁止覆盖。发布意图先持久记录，再执行无覆盖发布。进程恰在发布窗口中断时，可能已经存在完整文件但回执未落盘；此时返回 `UPLOAD_RECOVERY_REQUIRED`，不会仅凭同摘要目标自动认领为成功。

撤权会阻止后续请求和回执披露。已经被节点接受的最后一次提交存在分布式在途窗口，不能保证撤权能追溯取消该原子发布；不要将权限错误解释成“一定没有落盘”。

## 宿主兼容证据

| 宿主/入口 | 实际机制 | 本阶段证据与缺口 |
| --- | --- | --- |
| dot / ChatGPT 插件 | 顶层原生 fileParams → Hub 中转 | 原生参数在旧导入中确实被宿主转为下载来源；新中转使用合成隔离测试，真实新链路未验收 |
| Codex 的 codex_apps 插件 | 上游实现将本地文件通过受沙箱控制的上传改写为原生对象 | 有上游源码证据；新链路未用真实 Codex 客户端往返验收 |
| 普通 Codex MCP / 其他 stdio MCP | 本地 `codepier_upload_local_file` 适配器 → HTTP 分块 | 需要明确配置读根；隔离测试覆盖，不能假定普通 MCP 自动理解 fileParams |
| Work 插件宿主 | 只有实际暴露并处理原生 fileParams 才可走同一中转 | 必须真实客户端 feature-detect/往返验收，目前未验证 |
| 没有文件 API 或本地适配能力的宿主 | 无可验证字节来源 | 报能力缺口；不能编造 file_id、签名 URL 或把 sandbox 路径当服务器可下载链接 |

参考：[OpenAI 插件文件 API](https://developers.openai.com/plugins/reference#file-apis)、[Codex MCP 客户端](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/rmcp_client.rs)、[Codex 原生文件处理](https://github.com/openai/codex/blob/main/codex-rs/core/src/mcp_openai_file.rs)、[MCP 传输规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)。MCP JSON-RPC 本身没有通用“自动读宿主文件”的能力。

## 升级后由所有者启用

1. 按现有维护流程备份匹配版本的 Hub 数据、Agent 配置及状态，并等待活动写入完成。先更新 Hub，保留新开关关闭；再从新 Hub 更新各目标 Agent。
2. 在每个需要接收文件的 Agent 原配置的 `integrations` 对象中，仅合并 `"file_import_streaming": true`，保留其他配置。确认当前运行版本和 `workspace(operation="readiness")` 返回 `file_import.resumable_upload_enabled=true`。
3. 所有者审核原生来源范围后，在 Hub 部署环境设置以下三项，再按自己的部署流程重建/重启 Hub：

```dotenv
CODEPIER_FILE_IMPORT_STREAMING=true
CODEPIER_NATIVE_FILE_RELAY=true
CODEPIER_NATIVE_FILE_PROVIDERS=["openai_sediment"]
```

这三项会允许经过现有项目写权限校验的入站，并使该 Hub 的所有原生导入使用前述受限 OpenAI 子域族。没有审核来源时保持 provider 为 `[]`，只使用原有精确主机。它们不自动授予新项目权限，不打开任意 Internet 下载。发布包的 Compose 模板会转发这三项，默认仍为 `false/false/[]`；k3s 的 ConfigMap 同样默认关闭。修改环境必须实际进入运行进程，单改本机 shell 或下载新 ZIP 不等于生效。

若需自定义精确 `CODEPIER_NATIVE_FILE_HOSTS`，应在实际服务环境或 Compose override 中显式提供 JSON 数组。基础 Compose 不会把一个未设置的 HOSTS 变成空字符串，避免混淆“未配置”和“拒绝全部”的语义。`[]` 是有意不允许基础精确主机；已启用的 provider 仍独立生效。

Hub 下载缓冲位于 `HUB_DATA_DIR` 所在的可写数据卷，不使用 Compose 的 64 MiB `/tmp`。为最大四个 128 MiB 缓冲及数据库队列/WAL留足空间；Agent 的两倍预留预算见上文。进程重启会断开连接，先确认没有未决发布。

4. 刷新/重连客户端工具目录，再以合成文件测试。普通 MCP 客户端还需在其本地桥接环境配置已审核的 `CODEPIER_FILE_IMPORT_ROOTS`；原生插件入口不需要这个本地根设置，也不能替普通 MCP 客户端自动提供文件读取能力。
5. 核对最终 `created=true`、完整字节数和 SHA-256，再导入实际资料。只升级软件而不启用新通道，仍保持旧来源限制。

回退时先停止提交新文件并核对所有未决操作，再关闭新入口，按原维护流程恢复匹配的软件和完整备份。保留上传 journal、Hub 操作和暂存证据；不要为了回滚而删除未知状态文件，也不要回退已经交付的项目文件。关闭入口后不能承诺继续通过该入口恢复回执，需要在重新启用或维护核查后处理。

## 部署前确认与验收

1. 取得有权读取的完整节点清单和各节点版本/平台。不能用项目映射推定所有设备；权限拒绝需先解决，不能换接口规避。
2. 确认具体 Hub 和 Agent 目标、源码版本、备份与回滚、无活动任务时的升级/重启窗口。源码修改不等于发布权限。
3. 审查 Hub 原生来源策略及其全部项目作用域；确认是否启用命名 provider。Agent 只开认证分块即可接收 Hub 字节，不需要每节点逐地区扩域。
4. 确认节点私有暂存目录、空间预算、TTL、服务账户和本地客户端允许读取的根目录。不要自动上传个人凭据，也不要安装未批准依赖或创建新 token。
5. 先单节点合成文件灰度，覆盖 0 B、Unicode、未知扩展名、超过 256 KiB 分块、同名冲突、超限、断连重启、丢回执及 SHA-256；再验证实际 dot、ChatGPT、Codex、Work 入口。
6. 逐节点记录运行版本、开关、实际写入路径、字节数、完整摘要和回执；样本 JPEG/PDF 成功不等于所有客户端和平台通过。

恢复时先关闭入口阻止新请求，保留数据库和暂存以核查未决上传；不要把删库/清暂存当成回滚。已经成功写入的项目文件不会因回滚软件自动撤回。
