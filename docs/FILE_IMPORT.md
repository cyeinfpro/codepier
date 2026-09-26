# 从对话导入文件

CodePier 的附件导入把当前宿主提供的文件保存到已授权项目中。图片、ZIP 和普通二进制文件使用同一条传输路径；ZIP 不会因为扩展名而被来源校验拒绝，也不会自动解压或执行。

需要项目写权限和支持原生文件参数的宿主。宿主没有提供文件引用时，不能把聊天中的下载链接、`sandbox:` 地址或编造的 `file_id` 当成原生附件。

## 正确的调用位置

先通过 `workspace(operation="open", project="项目名")` 确认目标，再读取 `workspace(operation="help", tool="write", action="import")`。调用时：

- 顶层填写 `project`、`operation="import"`、`idempotency_key` 和宿主提供的 `file`；隔离工作目录还需要原 `workspace_id`。
- `options.path` 是项目内未使用的相对路径；已知源文件 SHA-256 时，可填写 `options.expected_sha256`。
- 不要把 `file` 放进 `options`。宿主的 `openai/fileParams` 处理顶层字段；嵌套输入仅为已持有完整原生文件对象的旧调用方保留兼容，新的调用和帮助不应生成这种形式。

接收端的原生文件对象包含 `download_url`、`file_id`，以及可选的名称、媒体类型和大小。这些值应由宿主提供，不需要用户复制签名链接、浏览器 cookie 或访问令牌。`file_id` 本身不是服务器可验证的来源证明，不能据此放行任意 URL。

父目录不存在时会创建；目标文件已存在时拒绝覆盖。成功回执包含路径、字节数、SHA-256 和 `created=true`。拿到 `operation_id` 或 `pending=true` 只说明请求已保存，不代表文件已经到达电脑。

## 来源策略与升级

来源策略定义在 [shared/file_sources.py](../shared/file_sources.py)。1.14.3 修复了真实原生文件指向 `oaisdmntprkoreacentral.blob.core.windows.net`、而旧默认列表只有两个主机时的兼容问题。新增的是经过宿主文件参数核验的**具体存储账户主机**，不是 `*.blob.core.windows.net`，也不是相似前缀的任意 Azure 账户。

本机 Agent 的 `integrations` 配置提供两种不同的选择：

| 配置项 | 含义 |
| --- | --- |
| 不设置 `file_hosts` | 使用当前版本内置的来源列表。配置验证不会把这份默认列表固化到保存文件中，因此升级后仍可获得经审查的默认更新。 |
| `file_hosts` | 显式替换基础列表，适合主动收紧策略。已有列表不会被升级静默扩大；空数组表示基础列表不允许任何主机。 |
| `extra_file_hosts` | 在所选基础列表上增加由本机所有者明确批准的精确主机，不会覆盖默认来源。 |
| `max_import_bytes` | 单文件大小限制，默认 128 MiB，允许配置 1 字节至 512 MiB。 |

例如，只有在本机所有者独立确认 `uploads.example.com` 是所用宿主的可信下载服务之后，才可将下面的片段合并到原 Agent 配置；不要覆盖其他配置：

```json
{
  "integrations": {
    "extra_file_hosts": ["uploads.example.com"]
  }
}
```

主机名会规范化大小写和末尾的 DNS 根点，并去重。配置不接受 URL、端口、账号、IP 字面量、通配符或无效 DNS 标签。显式 `file_hosts=[]` 和非空 `extra_file_hosts` 同时设置时，只有扩展列表中的主机会被允许；要完全禁止来源，两份列表都应为空。

旧配置中已经写出的主机列表按显式限制处理，即使其内容恰好等于旧版默认值，也不会猜测用户意图并扩大权限。需要恢复随版本维护的默认策略时，应由本机所有者核对后移除 `file_hosts`，而不是修改为空数组。

`workspace(operation="readiness", project="项目名")` 返回的 `file_import` 包括来源策略版本、当前生效主机和大小上限。`host_roundtrip="not_run"` 表示本次就绪查询没有实际传输宿主文件，不能当成下载验收通过。

来源判断和下载在 Agent 执行。因此更新 GitHub 源码或只更新 Hub，并不代表正在运行的旧 Agent 已获得新策略。应按 [Agent 生命周期](AGENT_LIFECYCLE.md) 更新相应电脑，再核对运行版本；Hub 更新用于获得新的工具帮助、审计摘要和网页错误处理。

## 失败后如何处理

先保存原操作号，使用 `process(operation="get", operation_ids=[原操作号])` 查看最终结果。新的错误信息区分了失败阶段：

| 结果 | 处理方式 |
| --- | --- |
| `ARTIFACT_SOURCE_DENIED` / `host_not_allowed` | 查看脱敏 `source_host`，先核对 Agent 版本及显式来源限制。未知来源须独立核验，不能照抄错误中的域名自动批准。 |
| `unsupported_scheme` / `invalid_url` | 重新通过宿主提供原生附件；不要把沙箱路径、文本链接或拼接地址作为下载源。 |
| `non_public_address` | 检查 Agent 的 DNS/网络配置。即使主机被允许，解析到回环、内网、链路本地或组播地址也会拒绝。 |
| `ARTIFACT_DOWNLOAD_FAILED`，HTTP 401/403/404/410 | 文件源拒绝访问或链接可能失效。重新选择文件，取得新的宿主引用；这些状态不单独证明具体的过期原因。 |
| HTTP 408/429/500/502/503/504 | 文件源暂时不可用或限流。确认旧操作已失败后再由用户重新提交，不自动重复请求。 |
| `ARTIFACT_NETWORK` / `dns_failed`、`tls_failed`、`connect_failed`、`transfer_failed` | 按阶段检查 DNS、证书、连通性或传输中断；不要关闭 TLS 校验。 |
| `ARTIFACT_SIZE` / `ARTIFACT_INTEGRITY` | 大小或摘要不匹配，未完整校验的临时文件不会作为目标交付。 |
| `ARTIFACT_DESTINATION_EXISTS` | 选择新文件名；导入不提供强制覆盖。 |

网页附件入口会展示明确失败的原因及经过限制的来源主机，并恢复输入控件。重新点击保存会取得新文件引用和新的请求键。对于连接中断、`unknown`、`needs_review` 等不确定结果，仍锁定原目标，只允许恢复原回执，避免重复写入。

审计摘要只保留来源协议、精确主机和可用的文件大小；不会保存到公开摘要中的信息包括签名查询参数、对象路径、下载 URL 和 `file_id`。错误中的 `request_sent=false` 指**被拒绝的那一个地址**没有被请求；重定向校验失败时，前一跳可能已经访问过。

## 保留的安全边界

导入要求 HTTPS、正常证书验证和 443 端口。DNS 答案在连接前全部校验，并连接到已经核验的具体公网单播地址；每一次重定向都使用同一来源策略。请求不携带浏览器 cookie 或额外 bearer 凭据，也不使用环境中的 HTTP 代理。

传输会检查声明长度、实际字节数、宿主可选大小和调用方可选摘要。读取流时检查时间限制，限制重定向次数，拒绝额外的 HTTP 内容压缩；失败清理临时文件。最终通过锚定目录和禁止覆盖的发布步骤落盘，避免把半文件、被替换的路径或旧文件报告为新的成功交付。

## 维护与验收

增加内置来源前，应使用不含个人数据的测试文件，通过真正的宿主原生参数确认精确来源，再审查其信任范围。不能从 `file_id`、文件扩展名、网络错误或域名中包含 `openai` 推断来源可信。

回归包含配置保存后升级、显式限制、近似域名、重定向、DNS/TLS、真实 HTTP 响应分帧、慢流、大小/摘要、无覆盖以及网页即时/延迟失败恢复。自动化测试使用临时项目和确定性传输，不包含真实下载票据。真实宿主往返需要另行记录实际结果，不能用模拟成功代替。

宿主文件参数的约定见 [OpenAI 官方插件参考](https://developers.openai.com/plugins/reference)。
