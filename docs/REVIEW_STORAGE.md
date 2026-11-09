# 审阅快照存储与配额恢复

审阅快照存放在执行 Agent 的 `state/coding-reviews`，同一 Agent 上的项目共享存储池。Hub/VPS 的磁盘空间不是这个池的字节配额。默认总量为 256 MiB、256 条记录；创建新基线时预留 32 MiB 给后续审阅，因而新基线的有效上限是 224 MiB。

新基线保留 24 小时，完成的审阅保留 7 天。旧版本创建的记录继续遵守原 `expires_at`，不会因升级被提前删除。后台每小时回收过期数据，保存时也会检查过期项。

## 正常任务流程

先通过 `project_query(open)` 读取当前项目规则。准备修改时用 `workspace(open, capture_baseline=true)` 建立基线；小任务可通过 `options.baseline_paths` 提供最多 128 个确切文件路径，包含尚未创建的文件：

```json
{
  "project": "example",
  "operation": "open",
  "capture_baseline": true,
  "idempotency_key": "example-before-edit-001",
  "options": {"baseline_paths": ["src/view.py", "assets/new.svg"]}
}
```

路径必须是项目相对文件路径，不能是目录、链接或受保护/排除路径。空列表维持原来的全项目捕获行为。指定路径后，基线与之后的每次比较都限定在该范围内；`coverage.complete=true` 仅表示该范围完整，不能当成全项目完整。范围外修改需要事先建立另一份合适的基线。

任务结束时调用 `read`：

```json
{
  "project": "example",
  "operation": "changes",
  "options": {"baseline_ref": "<原基线编号>", "finalize": true}
}
```

完成操作原子替换基线，保留同一个引用编号。重试返回原先已固定的结果，不会重新采样当前目录。普通 `read(changes)` 的 `finalize` 默认仍为 `false`，支持需要反复比较的既有流程；仅查看改动不会自动释放基线。

## 查看、释放和压缩

默认统一 MCP 入口使用 `query(action="changes", view="reviews", project=...)` 查看快照，使用 `act(action="baseline", view="manage", project=..., idempotency_key=..., args={"action":"release", "baseline_ref":"..."})` 释放基线；压缩时将 `args` 改为 `{"action":"compact"}`。按文件创建基线使用 `act(action="baseline", project=..., idempotency_key=..., args={"baseline_paths":["src/view.py"]})`。以下独立工具调用仍可通过显式 core 目录使用。

用 `project_query(help, tool="read", action="reviews")` 或 `project_query(help, tool="edit", action="reviews")` 读取最新精确契约。

- `read(operation="reviews", project=...)` 返回全 Agent 的数字配额、用量，以及当前项目、目录映射、设备和调用方绑定下的快照列表。列表支持 `options.offset` / `options.limit`，不会暴露其他调用方的快照编号或源码，也不会删除到期数据。
- `edit(operation="reviews", options={"action":"release", "baseline_ref":"..."}, idempotency_key=...)` 释放已结束或放弃任务的基线。需要当前写权限，不能释放其他调用方的记录或已经完成的审阅结果。先确认任务已经结束，不能把正在使用的基线当缓存清掉。
- `edit(operation="reviews", options={"action":"compact"}, idempotency_key=...)` 无损压缩当前绑定的旧快照，不改变编号、逻辑内容、权限和租期。

所有调用指定原项目和 `workspace_id`（如有）。原绑定属于已撤销凭据时，新凭据不会自动获得其快照访问权。待处理、断线或结果不确定时继续查询原 operation ID；释放操作只以原幂等键恢复，不能换键猜测重放。

## 压缩与去重

新快照将较大的可压缩文本保存为私有的 SHA-256 内容块。每个快照仍有独立清单，记录绑定、采样区间、覆盖范围、创建时间和到期时间。相同文本可以共享一个内容块，不会合并不同任务的审阅区间。

配额按清单与共享内容块的实际存储字节计费，同一块只计算一次。逻辑单文档 32 MiB、源捕获 8 MiB、2000 文件等安全边界保持生效，解压也有总量和完整性检查。

写入顺序为内容块落盘、清单原子发布、回收无引用内容块。跨进程存储锁覆盖发布、读取和回收；失败不丢失原基线。中断后残留的孤立块和临时块由后续回收处理。符号链接、硬链接和损坏内容会被拒绝。

新保存会优先共享已有内容。字节预算不足时，系统先尝试无损压缩有效的旧记录，再重新计费；不会把提前过期或丢弃记录当作恢复手段。记录数上限仍独立生效，压缩不能代替任务收尾。

## 配额错误与本机配置

`REVIEW_QUOTA` 的 Agent 回执及 Hub 同步错误保留经过白名单验证的数字字段：

| 字段 | 含义 |
|---|---|
| `quota_scope` | 当前为 `agent` |
| `used_bytes` / `records` | 本次到期回收和必要压缩后的已有用量 |
| `limit_bytes` / `limit_records` | 配置上限 |
| `completion_reserve_bytes` | 新基线为完成审阅留下的空间 |
| `effective_limit_bytes` | 本次保存实际可用的字节上限 |
| `required_bytes` | 本次成功后整个池预计占用的总字节，不是本次新增字节 |
| `next_reclaim_at` | 假设没有新增占用，现有到期计划何时足够容纳这个新基线；无法仅靠到期解决时为 null |

`retryable=false` 表示不要立即自动重放失败调用。`next_reclaim_at` 不是下一条记录的到期时间，也不是对未来空闲容量的保证。已有审阅的基线租期不能靠等待延长，因此比较/完成失败不会给出跨租期的重试承诺。

容量可在 Agent 配置中设置，不是在 Hub 设置环境变量：

```json
"review_storage": {"max_storage_mib": 1024, "max_records": 512}
```

只提供需要覆盖的字段。字节配置接受 64–65536 MiB，记录数接受 16–10000 的整数；默认仍为 256 MiB / 256 条。配置只改变容量，不改变已有租期或授权。设置容量前由节点维护者检查真实磁盘余量。提高容量是应急余量，正常收尾、局部捕获和内容去重才是减少持续占用的主要办法。

## 升级与回滚

先停止旧 Agent 和旧原生聊天工作进程，备份整个 `coding-reviews` 目录（包括内容块），再部署新 Agent；Hub 也要升级才能发现新管理接口和显示完整错误字段。不要混跑新旧存储写入者。

新格式保留逻辑内容校验和，并另存清单校验和。旧 Agent 会拒绝压缩清单，不会把内容引用当作文本继续处理。

需要回退到旧 Agent 时，在所有写入者停止后，使用新版本源码导出完整的旧格式副本：

```sh
python scripts/export_review_snapshots.py --state-dir /absolute/agent/state --output /absolute/private/review-inline-export
```

输出必须是尚不存在的目录；工具不修改源清单或内容块，保留包括过期记录在内的全部编号、内容和租期，输出目录权限为 0700。导出校验成功后，由维护者备份当前池并用导出目录替换，再启动旧 Agent。旧格式可能重新超过旧配额，不能把格式回滚等同于容量修复。

相关验证：`tests/test_review_storage_optimization.py`、`tests/test_review_store_lifecycle.py`、`tests/test_coding_integration.py`。
