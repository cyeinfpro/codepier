# 项目知识地图

默认 unified 目录通过 `act(action="knowledge")` 生成/刷新地图，通过 `query(action="knowledge")` 查询地图。显式 core、full 和 coding 目录保留 `project_knowledge_build` 与 `project_knowledge_query` 两个独立工具，旧名称仍可调用；只有 read 权限的凭据不会发现生成动作，直接调用也不能绕过权限。

这是一份轻量、项目本地的结构参考，不是完整代码索引、类型解析器或自动运行的代理。生成不调用模型、不执行项目代码、不调用付费服务，也不复制 AOCI 的实现。

## 生成或刷新

调用 `act`，默认 view 为 `build`：

```json
{
  "project": "ProjectAlpha",
  "action": "knowledge",
  "idempotency_key": "knowledge-refresh-001",
  "args": {"max_files": 2000}
}
```

- 必须具有项目 read + write 权限，并满足 Agent 当前目录写入策略。无需 execute 权限。
- `project` 是项目别名或 ID，`workspace_id` 可选，用于指定已经授权的工作区，不扩展权限。
- `max_files` 默认 2000，范围 1–5000。扫描还有单文件、总读取字节数和索引存储配额；检查结果的 `truncated`、`skipped_files`、`skipped_reasons` 和 `coverage`。
- 无 `entries` 时确定性提取结构摘要、符号名和导入关系。它们是启发式信息，不保证引用完整或语义正确。
- `expected_revision` 可省略；传 `new` 要求尚无地图，传 64 位小写 SHA-256 要求当前版本匹配。生成期间仍有一次发布 CAS，防止并发刷新覆盖新结果。
- `idempotency_key` 必填，8–128 字符。恢复相同调用时复用原键；有意刷新使用新键。遇到未确定的结果先查询原操作，不重复提交。

成功结果包含 `state: ready`、`revision`、生成时间、条目数、变化/复用数量和扫描范围。地图保存在 Agent journal 数据库中，按项目身份、实际根目录及目录身份隔离；不会在源码目录生成或修改说明文件。项目映射或工作区发生变化时，不复用其他根目录的地图。

### 可选摘要补充

同一生成接口可接收最多 32 个 `entries`：

```json
{
  "project": "ProjectAlpha",
  "action": "knowledge",
  "idempotency_key": "knowledge-summary-001",
  "args": {"entries": [{
    "path": "src/app.py",
    "expected_sha256": "<读取该源码得到的64位小写SHA-256>",
    "summary": "应用入口：组装路由并启动服务。",
    "related_paths": ["tests/test_app.py"]
  }]}
}
```

每项必须提供源码路径、当前源码 SHA 和最多 1200 字符的摘要；最多 16 个关联路径。路径必须为规范项目相对路径，不接受绝对路径、遍历、反斜杠或控制字符，实际访问还会重新校验项目、敏感路径、符号链接和硬链接策略。重复条目路径和重复关联路径会被拒绝。

只有源码仍匹配 SHA 时才接受摘要。摘要可能由调用者或代理编写，`origin` 标识为 `agent`，不因此成为事实或执行指令。疑似凭据文本会被拒绝。这个接口本身不会请求模型生成摘要。

## 查询

调用 `query`，默认 view 为 `query`：

```json
{
  "project": "ProjectAlpha",
  "action": "knowledge",
  "offset": 0,
  "limit": 8,
  "args": {"query": "路由", "paths": ["src/app.py"], "max_chars": 6000}
}
```

- 只需项目 read 权限，不写入或自动刷新地图。
- `query` 默认空字符串，最多 300 字符；`paths` 默认空列表，最多 16 个不同路径。可以省略这些筛选条件。
- `offset` 范围 0–5000；`limit` 默认 8，范围 1–40。
- `max_chars` 默认 6000，范围 512–16000，限制知识查询 JSON 内容。协议操作编号等外层元数据另计。预算不足时摘要可能整体省略，检查 `content_omitted`、`budget_limited`、`truncated` 和 `next_offset`。
- 可选 read 幂等键仅用于恢复原读取；想读取新状态，应省略该键或使用新键。

没有地图时返回 `state: missing`、`revision: new` 和空条目，不会创建表、隐式生成或伪装为已有知识。已生成时返回 `state: ready`、版本、匹配总数和分页信息。

每个选中条目会再次检查当前路径权限和源码 SHA。当前条目是 `current`；变化或删除的条目是 `stale` 或 `missing`，只保留路径、校验值和状态等元数据，不返回过期摘要。存在此类条目时 `refresh_required` 提醒调用者显式刷新。被当前保护策略拒绝的内容不再返回。这里只检查选中条目，不宣称整个仓库都已验证最新。

## 工作时自动参考

已生成的地图会作为有界、只读、非权威参考加入项目打开/上下文、相关文件读取及原生任务的上下文。读取不会触发模型、命令或自动构建，地图缺失时不影响正常工作。终端 PTY 按键流不注入上下文。

自动上下文与显式查询遵循同样的当前路径和源码 SHA 检查。知识摘要不能替代 AGENTS.md、当前用户指令或权限检查；摘要中的提示、链接和所谓授权都是不可信项目数据。操作前仍应读取相关源码。

## 帮助

`query(action="help", args={"action": "knowledge", "view": "query"})` 返回查询契约；有写权限时将 view 改为 `build` 获取生成契约。返回的 `unified_call` 指明实际工具、默认 view 和参数位置。除常用顶层字段外，原接口参数原样放入 `args`；不增加 operation 字段或包装为 options。

## 兼容与恢复

两个工具都是明确的原生接口，Hub/Agent wire epoch 为 2。旧 Agent 或未声明这些工具的节点会返回升级要求，不回退到 Shell、通用脚本或旧文件接口。正常排队、幂等记录、项目授权与原操作恢复规则继续适用。

查询走有界读取队列；生成保留写入授权和项目写入排他，不把写入伪装为只读。传输返回 pending 时，使用 `query(action="job", ids=["<原操作编号>"])` 跟踪原操作（旧 `task_query` 仍可用）。仓库中的功能不表示已部署，实际使用还需相应 Hub、Agent 和客户端工具目录均已更新。
