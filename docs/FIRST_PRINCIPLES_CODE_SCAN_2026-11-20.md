# CodePier (`MCP`) 第一性原理全链路代码扫描报告

- **扫描基线**：`main` @ `ba13db48788175626f5af63bc7dbe91c2fda30e4`（`v1.33.34`，Git 跟踪文件 1,579 个）
- **扫描日期**：2026-11-20
- **扫描方法论**：沿真实用户流程与运行期调用链，从第一性原理（业务目标与协议边界）出发，结合全仓 AST 依赖图 / 强连通分量（SCC）、HTTP/MCP 路由表、SQLite 255 张表读写闭环、环境变量透传矩阵、前后端静态资产清单与依赖锁开展核查。保留领域安全与容量常量，避免机械变量化；认定死代码前逐一核实动态反射、框架钩子、安装器/计划任务入口、CI 脚本及文档声明的测试契约；严格区分**已确认问题**与**待验证风险**，不凭猜测下结论。

---

## 1. 实际扫描范围与问题优先级汇总

### 1.1 实际扫描范围

| 扫描模块 / 目录 | 覆盖规模（Git 跟踪） | 串联核查的真实用户流程与调用链 |
| :--- | :--- | :--- |
| `hub/` | 207 个 Python 模块（顶层 85、`api/` 13、`collaboration/` 99、`gateway/` 10） | `/mcp` JSON-RPC 与 SSE 流、`query`/`act` 统一路由、`ToolRouter` 目录与网关碰撞校验、Hub↔Agent WebSocket 帧投递与幂等回执、单写 SQLite `StoreWorker`、OAuth/OIDC/IAM 鉴权、设置中心、协作室与微信/Telegram 渠道 |
| `agent/` | 88 个 Python 模块 + `config.example.json` | 守护进程启动与热重载、`runner.py` 执行编排与 Outbox `journal`、`CoreFiles`/`FileEngine` 读写备份、LSP 温池 `LspPool`、原生 CLI `chat_worker`、桌面控制与浏览器宿主桥 |
| `shared/` | 68 个 Python 模块 + `source_inventory.json` | Pydantic 输入/输出契约与冻结 Schema 缓存、环境变量解析、防 SSRF 下载校验、Aho-Corasick 敏感输出脱敏、命令策略与品牌迁移契约 |
| `web/` | 163 个前端文件（SPA 主脚本、`chat/`、`collaboration-room/`、`mcp-apps/`、`vendor/`） | 面板首屏与懒加载、PAT/OAuth 授权弹窗、设置中心搜索与分组、调用审计与队列面板、聊天与协作室状态同步、MCP App 内嵌构建物与 `ReleaseAssets` |
| 构建、部署与脚本 | `install.sh`、`Dockerfile`、`.dockerignore`、`compose.yml`、`deploy/*`（16 个）、`.env.example`、`requirements/*.in`、`scripts/*.py`（90 个）、`.github/workflows/*` | `bash install.sh` 容器探针与旧卷迁移、Docker/k3s 环境变量透传、哈希锁依赖树、独立自举脚本同步、CI 与仓库契约门禁 |

### 1.2 问题优先级汇总表（已确认 30 项 + 待验证风险 3 项）

| 编号 | 优先级 | 问题维度 | 核心位置 | 问题简述 |
| :--- | :---: | :--- | :--- | :--- |
| **ISS-00** | **P0** | 容器打包缺失 | `Dockerfile:14` · `deploy/install-hub.sh:146` | `Dockerfile` 漏拷 `migrate_hub_data.py` 与 `file_import_client.py`，执行 `bash install.sh` 或旧卷迁移必败 |
| **ISS-01** | **P1** | 配置未生效 / 硬编码 | `shared/unified_contracts.py:31` · `hub/runtime.py:2248` · `hub/unified_tools.py:351` | `unified` 默认 `wait=30` 旁路 `HUB_CALL_WAIT_SECONDS`，两段等待叠加至 `40s`，且 `wait=0` 仍阻塞 `10s` |
| **ISS-02** | **P1** | 硬编码 / 协议失配 | `scripts/mcp_stdio_bridge.py:33,144,180,509` | `unified` 省 `wait` 退化为 `0` 漏挂流参数、浮点 `wait` 归零、`act` 漏填幂等键、报错指向已废弃旧工具 |
| **ISS-03** | **P1** | 硬编码阈值 / 重复 I/O | `agent/core_files.py:45,73,93,236` · `shared/computer_media.py:70` | 1–16 MiB 文本可读不可改/不可瘦身；读 3–16 MiB PNG/JPEG 抛桌面截图异常；单次修改持锁读盘 4–5 次 |
| **ISS-04** | **P1** | 配置未生效 / 语法分裂 | `hub/gateway/service.py:90` · `hub/knowledge_maintenance.py:70` · `execution_policy.py:32` · `core_contracts.py:184` | 全仓 5 套环境变量解析（`GATEWAY=true` 变关、`AUTO_ENROLL=false` 仍开、拼错放行），`MCP_RESPONSE_BYTES` >50 KiB 被截断 |
| **ISS-05** | **P1** | 配置未透传 / 吞错风暴 | `compose.yml:12,81` · `hub/mcp_streams.py:30` · `hub/collaboration/config.py:41` | 基础 `compose.yml` 漏透传运行期变量且硬编码日志轮转；`mcp_streams` 私造浮点解析可致 100Hz 风暴或 `inf` 挂死；微信配置联动恒假 |
| **ISS-06** | **P1** | 配置半下线 / 死代码失真 | `web/settings.js:492` · `hub/settings_snapshot.py:82` · `web/app.js:2971,3054` | 隐藏 `access_defaults` 卡片但保留快照与 `app.js` 权限预勾选，致计数 `+1`、搜“预选”空白且历史预选无法关闭 |
| **ISS-07** | **P1** | 重复冗余 / 异常契约分叉 | `hub/collaboration/network.py:78` · `room_weixin_native.py:142` · `telegram_protocol.py:65` · `file_download.py:145` | 4 处公网 IP/DNS/`Retry-After` 分叉：0 字节建连前 DNS 失败误锁微信扫码槽位、禁重发 Telegram；`file_download` 漏检 `ipv4_mapped` |
| **ISS-08** | **P1** | 死代码 / 丢弃控制帧 | `hub/runtime.py:411,842,4311` | `Connection.flush_control` 生产 0 调用；停机排空窗口与 `disconnect_device` 直接取消写任务丢弃待发 ACK |
| **ISS-09** | **P1** | 无用依赖 / 依赖错置 | `web/mcp-apps/package.json:12` · `web/vendor/*` · `requirements-dev.in:9` | 仅测试用的 `ext-apps` 置于生产 `dependencies` 致清单与文档失真；退役 317 KB `@xterm/*` 入镜像、`js-sha256` 漏登、`jsonschema` 重复 |
| **ISS-10** | **P2** | 硬编码不对称 | `hub/mcp.py:567` · `hub/oauth.py:54` · `compose.yml:69` | `/mcp` `Origin` 白名单与 `OAUTH_REDIRECT_HOSTS` 5 处硬编码不对称（`platform.openai.com` vs `gemini`/`antigravity`） |
| **ISS-11** | **P2** | 过度耦合 | `hub/runtime.py:560` · `agent/runner.py` · `hub/token_dashboard.py:658` | 千行上帝模块、27 组双向循环与 `hub/` 89 模块单一 SCC、穿透标准库 `Queue._queue` 及 60 处跨文件私有属性访问 |
| **ISS-12** | **P2** | 不必要的抽象（模块层） | `agent/project_index.py` · `hub/auth_schema.py:16` · `shared/workflow_templates.py` | 单调用方微模块与宿主双向成环、`auth_schema` 重复建已有索引、仅 Hub 使用的工作流模板错放 `shared/` |
| **ISS-13** | **P2** | 不必要的抽象（垫片/路由） | `agent/relay_*.py:6` · `service_watchdog.py:102` · `room_runtime_api.py:8-92` | `import *` 垫片吞掉非零退出码返 `0`；协作室拆 4 挂载器且 `room_runtime_api` 连续双调 `store.run` 争锁并漏 `Cache-Control` |
| **ISS-14** | **P2** | 重复冗余逻辑 | `hub/tool_router.py:105` · `shared/contracts.py:1839` · `hub/roles.py:462` 等 | `ToolRouter` 绕过 Schema 缓存且 `"full"=="core"` 占 6 槽；11 处同步端点双鉴权；canonical JSON、SSE 与 `shield` 样板重复 |
| **ISS-15** | **P2** | 死代码 / 黑洞表 | `hub/collaboration/chatroom.py:1089` · `message_handling.py:41,171` · `chat_worker.py:1124` | `Chatroom.read` 尾部 ~50 行及 2 辅助方法 100% 不可达；`dot_message_coverage` 表只写不读；`chat_worker` 长度分支恒假 |
| **ISS-16** | **P2** | 配置未生效 / 运维缺陷 | `agent/lsp_navigation.py:771,795` · `integration_config.py:114` · `deploy/k3s/deployment.yaml:20` | `LspPool` 未接 `config.json` 且切 loop 泄漏语言服务子进程；k3s 备份注释漏 `--keys-output` 执行必败 |
| **ISS-17** | **P3** | 死代码 | `web/call-log.js:151-155` | 前端格式化函数 `timeOnly(n)` 全仓 0 调用 |
| **ISS-18** | **P3** | 死代码 | `hub/store.py:990-991` | `Store.async_security_epoch` 全仓 0 调用 |
| **ISS-19** | **P3** | 死代码 | `hub/operation_history.py:380-382` | `rewrite_task_ciphertext` 生产 0 调用（已被 `task_rekey_session` 取代） |
| **ISS-20** | **P3** | 死代码 | `hub/unified_tools.py:72-73` | `visible_actions` 生产 0 调用 |
| **ISS-21** | **P3** | 死代码 | `hub/collaboration/channel_media_router.py:110-116` | `ChannelMediaRouter.cache_usage` 全仓 0 调用 |
| **ISS-22** | **P3** | 死代码 | `hub/connection_evidence.py:75-78` | `ConnectionEvidence.reset_dedup` 生产 0 调用 |
| **ISS-23** | **P3** | 死代码 | `shared/secret_output.py:6-35` | 单模式脱敏类 `SecretOutput` 生产 0 调用（已被 `SecretOutputs` 取代） |
| **ISS-24** | **P3** | 死代码 | `agent/runner.py:810-816` | 同步阻塞方法 `Agent.cancel_pending_call` 生产 0 调用 |
| **ISS-25** | **P3** | 死代码 | `agent/lsp_navigation.py:808-813,871-877` | `LspPool.resident_count` 与 `LspPool.evict_idle` 生产 0 调用 |
| **ISS-26** | **P3** | 死代码 | `scripts/benchmark_five_directories.py:1-217` | 孤儿压测脚本全仓 0 引用且调用已移除工具 `project_query` 必报错 |
| **ISS-27** | **P3** | 死代码 | `scripts/mcp_stdio_bridge.py:32` | 集合常量 `REMOTE_TOOLS` 脚本内 0 使用 |
| **ISS-28** | **P3** | 死代码 | `hub/integrations.py:385-415` | 同步方法 `HubIntegrations.capture_output` 生产 0 调用 |
| **ISS-29** | **P3** | 死代码 / 同名异构 | `shared/context_estimate.py:346-380` | `context_estimate.reference_cost` 生产 0 调用且与 `token_cost.reference_cost` 同名异构 |
| **RSK-01** | **P1（待验）** | 旧接口退役风险 | `hub/collaboration/api.py:20-90,151-467` · `room_api.py:69-71` | 12 个旧 GET 路由 + `POST /{operation}` 66 个旧管理操作 + `POST /connections/route-prepare` 仓内生产零引用 |
| **RSK-02** | **P2（待验）** | 兼容入口退役风险 | `agent/relay_*.py` · `scripts/relay_*.py` · `mcp_stdio_bridge.py:400` | 4 个旧品牌 `relay_*` 垫片及旧 MCP Profile（`core`/`full`/`coding`）与网桥默认 `core` 存量兼容窗口 |
| **RSK-03** | **P3（待验）** | 孤立诊断探针风险 | `hub/api/system.py:82-93,160-170` | `GET /api/system/health` 与 `GET /api/system/collaboration-telemetry` 仓内生产零调用 |

---

## 2. 已确认问题详细分析

### 2.1 P0 阻断级问题

#### `ISS-00` [容器打包缺失] `Dockerfile` 漏拷 `scripts/migrate_hub_data.py` 与 `scripts/file_import_client.py` 阻断标准安装与迁移
- **文件路径与行号**：`Dockerfile:14`、`deploy/install-hub.sh:146-152`、`scripts/migrate_hub.py:122`、`scripts/mcp_stdio_bridge.py:356`、`tests/test_hub_image_permissions.py:70-73`
- **代码证据**：
  ```dockerfile
  # Dockerfile:14 —— 仅白名单拷入 3 个脚本，缺失 migrate_hub_data.py 与 file_import_client.py
  COPY --chown=root:root scripts/install_agent.py scripts/agent_lifecycle.py scripts/mcp_stdio_bridge.py ./scripts/
  ```
  ```bash
  # deploy/install-hub.sh:146-152 —— 容器内导入缺失模块返回 1 (!= 3)，直接报错终止安装
  COMPOSE_FILE="$probe_compose" docker compose run --rm --no-deps ... --entrypoint python hub \
    -c 'import os,sys; from scripts.migrate_hub_data import probe_admin; ...' || probe_status=$?
  if ((probe_status != 3)); then echo '无法检查管理员或数据卷；没有尝试重新初始化。' >&2; exit "$probe_status"; fi
  ```
- **触发条件**：执行官方标准安装/升级入口 `bash install.sh`（或 `./codepier install-hub`），或触发 `scripts/migrate_hub.py` 旧数据卷迁移，或在容器内通过 `scripts/mcp_stdio_bridge.py` 执行本地文件导入。
- **实际影响**：标准 Docker 安装与升级在 `probe_admin` 探针阶段 100% 报 `ModuleNotFoundError: No module named 'scripts.migrate_hub_data'`（退出码 `1 != 3`）并中止安装，面板无法初始化或升级。
- **优先级**：**P0**
- **具体优化方案**：在 `Dockerfile:14` 补齐 `scripts/migrate_hub_data.py` 与 `scripts/file_import_client.py`（同步更新 `tests/test_hub_image_permissions.py:70-73` 白名单断言），并在 `.github/workflows/image.yml` 接入 `scripts/check_codepier_migration_docker.py` 实跑候选镜像安装演练。

---

### 2.2 P1 高优先级问题

#### `ISS-01` [配置未生效 / 硬编码] `HUB_CALL_WAIT_SECONDS` 被 `unified` 旁路且两段等待叠加至 `40s`，`wait=0` 仍阻塞 `10s`
- **文件路径与行号**：`.env.example:22-23`、`shared/config.py:13,106`、`shared/unified_contracts.py:31,555-562`、`hub/runtime.py:475,1776-1809,2245-2252`、`hub/unified_tools.py:348-392`、`hub/mcp.py:94-96,828-862`
- **代码证据**：
  ```python
  # shared/unified_contracts.py:31 —— 硬编码 30s，未读取 HUB_CALL_WAIT_SECONDS
  DEFAULT_WAIT_SECONDS = 30
  # hub/runtime.py:2248 —— 首轮无条件按 self.wait_seconds (默认 10s) 等待
  result = await asyncio.wait_for(asyncio.shield(future), timeout=self.wait_seconds)
  # hub/unified_tools.py:351,384 —— 返回 pending 后未扣除首轮耗时，再叠加等待 plan.wait (30s)
  deadline = time.monotonic() + plan.wait
  ```
- **触发条件**：修改环境变量 `HUB_CALL_WAIT_SECONDS` 后通过默认 `unified` 目录调用 `query`/`act`；或调用非 `PROCESS_TOOLS` 操作（如慢速 `file`/`tree`/同步 `search`/`edit`/`lsp`）及 `vps:*` `exec`（默认 `yield_seconds=10`）耗时超过 `10s`；或显式传入 `wait=0`。
- **实际影响**：`HUB_CALL_WAIT_SECONDS` 对默认 `unified` 入口不生效；慢调用在首轮 `10s` 超时返回 `pending` 后，`_wait` 重新计算 `plan.wait`，总阻塞时间叠加至 `self.wait_seconds + plan.wait`（默认 `40s`）；显式传 `wait=0` 时仍被首轮阻塞 `10s`，且 `hub/mcp.py:95` 因 `requested_wait_seconds == 0.0` 漏挂 SSE 保活与取消定时器。
- **优先级**：**P1**
- **具体优化方案**：在 `hub/unified_tools.py:375` 执行前记录 `started = time.monotonic()`，向 `runtime.invoke` 传入受 `plan.wait` 约束的首轮等待上限（使 `wait=0` 立即返回持久回执），并在 `_wait` 中按 `started + plan.wait` 扣减剩余预算；明确或对齐 `HUB_CALL_WAIT_SECONDS` 与 `DEFAULT_WAIT_SECONDS` 关系并更新 `.env.example:22-23`。

#### `ISS-02` [硬编码 / 协议失配] `mcp_stdio_bridge` 省略 `wait` 失配、浮点 `wait` 归零、`act` 漏填幂等键且恢复指引指向废弃工具
- **文件路径与行号**：`scripts/mcp_stdio_bridge.py:33-35,139-163,176-185,398-409,499-529`、`.env.example:114`、`shared/unified_contracts.py:31,326,555-561`
- **代码证据**：
  ```python
  # scripts/mcp_stdio_bridge.py:33, 176-184, 509-513
  IDEMPOTENT_TOOLS = {name for name, tool in TOOLS.items() if ...}  # 不含 unified "act"
  requested_wait = arguments.get("wait", arguments.get("wait_seconds", 0)) if isinstance(arguments, dict) else 0
  wait = min(60, max(0, requested_wait)) if type(requested_wait) is int else 0
  if profile == "unified" and wait > 10:
      endpoint += "&stream=1"
  recovery_tool = ("workflows_get" if workflow_id else "workflows_list") if workflow_call else "operations_list"
  ```
- **触发条件**：配置 `CODEPIER_MCP_PROFILE=unified` 通过 stdio 网桥调用且省略 `wait`（服务端默认等 `30s`）；或传入浮点 `wait`（如 `15.0`）；或调用 `act` 省略 `idempotency_key` 发生断线重试；或传输重试耗尽返回 `-32000` 错误。
- **实际影响**：`unified` 省略 `wait` 时网桥按 `0` 计算，漏挂 `&stream=1`（反向代理缓冲下延迟 `5s` 才发流式头）且 `attempt_timeout=20s` 短于服务端 `30s` 默认等待导致误超时重发；浮点 `wait` 被 `type(...) is int` 清零；`IDEMPOTENT_TOOLS` 不含 `act` 导致未自动生成 `bridge-<uuid>` 幂等键；错误恢复提示指向已移除的 `operations_list`/`workflows_*`；`.env.example:114` 漏列 `unified`。
- **优先级**：**P1**
- **具体优化方案**：`unified` 模式下省略 `wait`（且非即时本地动作）时对齐服务端默认 `30` 并附加 `&stream=1`；将 `type(requested_wait) is int` 改为 `isinstance(requested_wait, (int, float)) and not isinstance(requested_wait, bool)`；为需要幂等键的 `act` 动作补齐 `bridge-<uuid>` 生成逻辑；按当前 `profile` 输出 `query(action="job")` 恢复指引并更新 `.env.example:114`。

#### `ISS-03` [硬编码阈值 / 重复 I/O] 文件读写上限失配（16 MiB 读 vs 1 MiB 写/改）、读 3–16 MiB PNG/JPEG 抛桌面截图异常及单次修改 4–5 次读盘
- **文件路径与行号**：`agent/core_files.py:44-46,73-115,234-248`、`agent/filesystem.py:365-398,1244-1316,1399-1434`、`shared/computer_media.py:12-79`、`agent/file_blobs.py:61`
- **代码证据**：
  ```python
  # agent/core_files.py:45, 73, 93-107, 236
  if len(text) + growth > 1048576:
      raise DevError("FILE_TOO_LARGE", "替换后的文件超过 1 MiB")
  data = engine.read_bytes(path, max_bytes=16 * 1024 * 1024)
  if mime:
      from shared.computer_media import normalize_content
      return {**meta, **normalize_content({"content": [{"type": "image", ...}]})}  # >3 MiB 抛 COMPUTER_IMAGE_LIMIT
  before = engine.read_bytes(path, True)  # 默认 MAX_FILE=1 MiB，旧文件 >1 MiB 连覆写瘦身都失败
  ```
- **触发条件**：对 `1–16 MiB` 的项目文本文件执行局部 `edit` 或覆写瘦身；读取 `3–16 MiB` 的项目 PNG/JPEG 图片；执行常规 `write`/`edit` 落盘。
- **实际影响**：`1–16 MiB` 文本文件可读拿 SHA，却无法通过 `edit` 修改，甚至无法用 `write` 覆写为更小文件（且报错提示引导只授读写权限的用户调用 `exec`）；读取 `3–16 MiB` 项目 PNG/JPEG 直接抛出误导性异常 `COMPUTER_IMAGE_LIMIT: 原生截图类型或体积超出限制`（而 `>16 MiB` PNG 或 `4 MiB` WebP 反而正常降级返回 `asset_metadata`）；每次写入在 `CoreFiles.call` 与 `FileEngine._mutate` 同一锁内重复全量读取文件并计算哈希达 4–5 次。
- **优先级**：**P1**
- **具体优化方案**：在 `agent/core_files.py:93` 对 `len(data) > MAX_IMAGE_BYTES`（3 MiB）的图片降级返回 `asset_metadata`；将 `>1 MiB` 旧文件备份接入流式 `file_blobs.backup`，统一按 UTF-8 字节数放宽局部 `edits` 上限；在 `CoreFiles.call` 与 `FileEngine._mutate` 间复用已校验字节与 SHA，消除锁内冗余全量读盘。

#### `ISS-04` [配置未生效 / 语法分裂] 全仓 5 套分裂的环境变量解析规则，且 `CODEPIER_MCP_RESPONSE_BYTES` 调大超 50 KiB 被 Agent 截断
- **文件路径与行号**：`hub/gateway/service.py:90`、`hub/knowledge_maintenance.py:70`、`shared/execution_policy.py:32-40`、`hub/file_import_settings.py:127-131`、`hub/collaboration/weixin.py:69`、`shared/core_contracts.py:184`、`agent/core_files.py:13`
- **代码证据**：
  ```python
  # hub/gateway/service.py:90 vs hub/knowledge_maintenance.py:70 vs shared/execution_policy.py:32
  self.enabled = os.getenv("CODEPIER_MCP_GATEWAY", "1") == "1" ...          # 填 true 静默变 False
  self.auto_enroll = os.getenv("CODEPIER_KNOWLEDGE_AUTO_ENROLL", "1") != "0" # 填 false 静默保持 True
  return os.getenv("MCP_BLOCK_LOCAL_CODEX", "").strip().lower() in {"1", "true", "yes", "on"}  # 拼错静默放行
  # shared/core_contracts.py:184 允许配至 1 MiB，但 agent/core_files.py:13 写死 OUTPUT_BYTES = 50 * 1024
  READ_BATCH_PAYLOAD_BYTES = env_int("CODEPIER_MCP_RESPONSE_BYTES", 48 * 1024, 8 * 1024, 1024 * 1024)
  ```
- **触发条件**：运维按 `.env.example` 常见布尔习惯填写 `CODEPIER_MCP_GATEWAY=true`、`CODEPIER_KNOWLEDGE_AUTO_ENROLL=false`、误拼 `MCP_BLOCK_LOCAL_CODEX`、用逗号分隔 `CODEPIER_WEIXIN_APPROVED_API_BASES`，或将 `CODEPIER_MCP_RESPONSE_BYTES` 调大至 `>51200`。
- **实际影响**：网关被意外关闭、自动知识维护无法关闭、本地 Codex 封禁开关拼错时 Fail-Open 放行（与同文件 `:36` Fail-Closed 策略相反）、微信白名单基址按空白 `.split()` 与全仓逗号 `env_csv` 割裂；单文件读取被 Agent 端硬编码 `OUTPUT_BYTES = 50 * 1024` 提前截断，导致调大 Hub 响应预算不生效。
- **优先级**：**P1**
- **具体优化方案**：将全仓布尔环境变量统一收口至 `shared.config.env_bool`（非法值启动期抛 `ConfigurationError`）；`CODEPIER_WEIXIN_APPROVED_API_BASES` 兼容逗号与空白分隔；将单次文件输出上限与 `CODEPIER_MCP_RESPONSE_BYTES` 对齐或按调用参数下发预算。

#### `ISS-05` [配置未透传 / 吞错风暴] 基础 `compose.yml` 漏透传 7 项变量与日志轮转，流控配置私造解析器可致 100Hz 风暴或挂死，协作微信联动校验恒假
- **文件路径与行号**：`compose.yml:12-69,81-82`、`.env.example:48-54`、`hub/mcp_streams.py:21-38,58-74`、`hub/collaboration/config.py:35-60`、`hub/store.py:529`、`hub/mcp.py:184`、`hub/connection_evidence.py:59`、`deploy/k3s/deployment.yaml:42-70`、`scripts/check_repository_contracts.py:32-64`
- **代码证据**：
  ```python
  # hub/mcp_streams.py:30-37 —— 未用 shared.config.env_float，漏检 math.isfinite 与上限且吞掉非法值
  def _env_float(name, default, *, minimum=0.01):
      raw = os.getenv(name)
      ...
      return max(minimum, float(raw))  # "-1"/"nan"/"-inf" -> 0.01s；"+inf" -> 永久挂死
  # hub/collaboration/config.py:41, 54 —— 先算 enabled and weixin_enabled，致 :54 result.weixin_enabled 恒假
  ```
- **触发条件**：标准 Docker Compose 用户在 `.env` 配置 `CODEPIER_SQLITE_SYNCHRONOUS`、`CODEPIER_MCP_DEFAULT_VIEW`、`CODEPIER_EVIDENCE_DEDUP_SECONDS`、4 个 `CODEPIER_MCP_*STREAM*`/`SSE*` 变量或 `CODEPIER_LOG_MAX_SIZE`/`FILES`（未额外叠加 `compose.performance.yml`）；或流控变量填入负数/`NaN`/`inf`；或仅设 `CODEPIER_WEIXIN_ENABLED=true` 漏开 `CODEPIER_COLLABORATION_ENABLED=true`。
- **实际影响**：7 项运行期参数与 2 项日志轮转配置（`compose.yml:81-82` 硬编码 `10m`/`3`）在基础 Compose 部署下不生效；`mcp_streams.py` 畸形浮点值会触发每连接 100Hz SSE 保活风暴或 `inf` 挂死；显式开启微信但漏开协作总开关时被静默吞为关闭而不报 `ConfigurationError`。
- **优先级**：**P1**
- **具体优化方案**：`hub/mcp_streams.py` 改用 `shared.config.env_int`/`env_float`；`CollaborationConfig.from_env` 在与 `enabled` 合并前校验显式 `weixin_enabled`；在 `compose.yml` 补齐 7 项环境变量透传并将 `:81-82` 改为 `${CODEPIER_LOG_MAX_SIZE:-10m}` / `${CODEPIER_LOG_MAX_FILES:-3}`，同步补齐 k3s ConfigMap 与 `scripts/check_repository_contracts.py` 契约门禁。

#### `ISS-06` [配置半下线 / 死代码失真] 隐藏 `access_defaults` 设置卡片却残留后端快照与 `web/app.js` 权限预勾选，致计数虚高与隐式扩大预选
- **文件路径与行号**：`shared/settings_catalog.py:64-77`、`hub/settings_snapshot.py:8,72-88`、`hub/api/settings.py:288`、`hub/oauth.py:440`、`hub/access.py:15-28,114-169`、`web/settings.js:58-97,260-296,491-494,532-587`、`web/app.js:2971,3003,3054`
- **代码证据**：
  ```javascript
  // web/settings.js:492 隐藏卡片，但 :537 filter() 仍统计全量 items；web/app.js:2971,3054 仍读取旧快照预勾选
  data.items.filter((item) => item.id !== 'access_defaults' && !commonItems.has(item.id)).map(card)
  // web/app.js:3054
  all_projects: remembered.all_projects ?? r.access_defaults?.all_projects === true,
  ```
- **触发条件**：打开设置页查看“更多设置”计数或搜索 `默认授权`/`预选`/`developer_scopes`；或历史库表存有 `mcp_access_defaults:<user_id>` 的用户打开新建 PAT / OAuth 授权弹窗。
- **实际影响**：`#settings-more-count` 恒虚增 `+1`，搜索 `预选` 提示匹配 1 项但列表空白，`settings_snapshot.py:82-88` 失真声明 `editable_here=True`；`PUT /api/settings/access` 前端零调用悬空；曾开启旧默认预选的用户在 PAT/OAuth 弹窗中仍被隐式预勾选 `write`/`execute` 与 `all_projects`，且面板已无开关可关闭。
- **优先级**：**P1**
- **具体优化方案**：彻底完成 `v1.33.3` 下线闭环：从 `settings_catalog.py`、`settings_snapshot.py`、`oauth.py:440`、`web/settings.js:492` 及 `web/app.js:2971,3003,3054` 移除 `access_defaults` 读取（仅保留浏览器本地按客户端记忆的 `codepier-oauth-choice`），同步更新 `tests/test_settings_center.py:85` 并下线孤立写接口 `PUT /api/settings/access`。

#### `ISS-07` [重复冗余 / 异常契约分叉] 4 处公网 IP / DNS / `Retry-After` 实现分叉，建连前 DNS 失败误锁微信扫码槽位与禁重发 Telegram
- **文件路径与行号**：`hub/collaboration/network.py:28-40,76-128`、`hub/gateway/network.py:74-113`、`hub/collaboration/weixin_protocol.py:211-260,296-306`、`hub/collaboration/telegram_protocol.py:6,52-71`、`hub/collaboration/room_weixin_native.py:131-147`、`shared/file_download.py:142-151`、`hub/collaboration/events.py:1684-1705`
- **代码证据**：
  ```python
  # hub/collaboration/room_weixin_native.py:141-146 —— DNS socket.gaierror 非 DevError，0 字节未发出却判为响应未确认
  error = (
      {"code": exc.code, "status": exc.status}
      if isinstance(exc, DevError)
      else {"code": "WEIXIN_QR_RESPONSE_UNCONFIRMED"}
  )
  ```
- **触发条件**：微信原生房间扫码或 Telegram 发送在 TCP/TLS 建连前遇到瞬时 DNS 解析失败（`socket.gaierror`）；`shared/file_download.py` 解析到 IPv4-mapped IPv6 私网/保留地址；远端 429 返回超大 `Retry-After`。
- **实际影响**：0 字节发出的建连前 DNS 故障在 `room_weixin_native.py:145` 被误判为 `WEIXIN_QR_RESPONSE_UNCONFIRMED` 锁死扫码槽位，在 `telegram_protocol.py:65-71` 被吞为 `TELEGRAM_TRANSPORT_UNKNOWN` 禁止自动重试；`shared/file_download.py:145-151` 漏检 `ip.ipv4_mapped` 与 `ip.is_reserved`；`weixin_protocol.retry_after`（`:298`）允许高达 `999,999,999s` 退避且被 `telegram_protocol.py:6` 跨渠道反向导入。
- **优先级**：**P1**
- **具体优化方案**：在 `shared/` 收敛纯函数 `is_public_unicast_ip()`（统一展开 `ipv4_mapped` 并校验 `not is_reserved`）与有界 `parse_retry_after(max_seconds=86400)`；在渠道协议层显式区分建连前 `OSError`（`request_sent=False`，可安全重试）与已发包后的响应未确认异常。**注意**：严禁在 `hub/collaboration/network.py` 将 `OSError` 直接包装为普通 `DevError`，否则 `events.py:1684-1702` 会把单次 DNS 抖动误判为 `authorization_revoked` 永久吊销事件订阅。

#### `ISS-08` [死代码 / 丢弃控制帧] `Connection.flush_control` 生产零调用，停机 HTTP 排空期与踢设备时直接取消写任务丢弃待发 ACK
- **文件路径与行号**：`hub/runtime.py:411-430,839-845,4264-4318`
- **代码证据**：
  ```python
  # hub/runtime.py:411-430 实现了带哨兵排空高优控制帧的 flush_control，但生产 0 调用
  async def flush_control(self, wait_seconds: float = 1.0) -> None:
      """Drain queued priority control frames (e.g., ACKs) before WebSocket detach."""
  # hub/runtime.py:4311 close_connection 与 :842 disconnect_device 直接 c.close() 取消 _writer_task
  ```
- **触发条件**：Hub 滚动停机在 `announce_shutdown()` 完成后至 `_stop_runtime()` 置 `self.stopping = True` 前的 HTTP 排空窗口内收到 Agent 结果并入队 `ack`；或管理员调用 `disconnect_device` 瞬间控制队列仍有待发 `ack`。
- **实际影响**：Hub 已落库的操作结果 `ack` 在写出到 WebSocket 前随 `_writer_task` 取消被丢弃，导致 Agent 重连后从本地 Outbox 重复补传历史回执，`flush_control` 沦为死代码。
- **优先级**：**P1**
- **具体优化方案**：在 `_stop_runtime` 的 `close_connection()` 调用 `c.close()` 前，以及 `disconnect_device()` 解绑关闭前，对健康连接执行 `await connection.flush_control(wait_seconds=1.0)`。

#### `ISS-09` [无用依赖 / 依赖错置] 仅测试使用的 `ext-apps` 错置生产 `dependencies`，退役 317 KB `@xterm/*` 入镜像、`js-sha256` 漏登且 `jsonschema` 重复声明
- **文件路径与行号**：`web/mcp-apps/package.json:11-22`、`web/mcp-apps/build.mjs:7-95`、`tests/fixtures/mcp-apps-host.js:1`、`docs/INTEGRATIONS-20260917.md:159`、`web/integration-guide.md:159`、`web/vendor/manifest.json:1-17`、`web/index.source.html:32`、`Dockerfile:11`、`.dockerignore:1-40`、`tests/support_legacy_terminal.py:24-35`、`requirements/requirements-dev.in:9`
- **代码证据**：
  ```json
  // web/mcp-apps/package.json:11-16 —— changes-v1.html 仅打入本地 host-bridge.js，ext-apps 仅供 tests/fixtures 使用
  "dependencies": { "@modelcontextprotocol/ext-apps": "2.0.4" },
  "devDependencies": { "esbuild": "0.28.2", "prettier": "3.10.1" }
  ```
- **触发条件**：执行前端构建、Docker 镜像构建（`Dockerfile:11` 整目录 `COPY web ./web`）、源码包归档或第三方许可证审计。
- **实际影响**：`@modelcontextprotocol/ext-apps` 未进入生产 `changes-v1.html` 却被 `build.mjs` 写入构建清单与 53 KB `THIRD_PARTY_NOTICES.txt`（两份集成文档亦失真宣称生产打包该 SDK）；已退役的 317 KB `web/vendor/{xterm-5.5.0,addon-fit-0.10.0,addon-search-0.15.0}` 以及 `web/**/*.test.mjs`、`build*.mjs` 被打入生产镜像，而生产 `index.source.html:32` 实际加载的 `js-sha256-1.0.0` 反漏登 `web/vendor/manifest.json`；`requirements-dev.in:9` 重复声明已通过 `-r requirements.in` 继承的 `jsonschema==4.26.0`。
- **优先级**：**P1**
- **具体优化方案**：将 `@modelcontextprotocol/ext-apps` 移入 `devDependencies`（保留安全 `overrides` 以满足 `test_mcp_sdk_dependency_security.py`），修正 `build.mjs`、`THIRD_PARTY_NOTICES.txt`、`test_generated_release_hygiene.py` 与两份集成文档；将 `@xterm/*` 迁至 `tests/fixtures/legacy-terminal/vendor/`，在 `web/vendor/manifest.json` 补登 `js-sha256-1.0.0`，在 `.dockerignore` 排除 `web/**/*.test.mjs` 与 `web/mcp-apps/build*.mjs`；删去 `requirements-dev.in:9` 重复行。

---

### 2.3 P2 中优先级问题

#### `ISS-10` [硬编码不对称] `/mcp` `Origin` 白名单与 `OAUTH_REDIRECT_HOSTS` 5 处硬编码集合不对称
- **文件路径与行号**：`hub/mcp.py:567-579`、`hub/oauth.py:54-62`、`.env.example:14`、`compose.yml:69`、`deploy/k3s/deployment.yaml:53`
- **代码证据**：
  ```python
  # hub/mcp.py:567-579 含 gemini.google.com / antigravity.google.com，但缺 platform.openai.com
  allowed = { ..., "https://chatgpt.com", "https://gemini.google.com", "https://antigravity.google.com", ... }
  # hub/oauth.py:57 含 platform.openai.com，但缺 gemini.google.com / antigravity.google.com
  ```
- **触发条件**：从 `https://platform.openai.com` 发起带 `Origin` 头的 `/mcp` 预检/调用，或从 `gemini.google.com` / `antigravity.google.com` 发起默认 OAuth 回调。
- **实际影响**：OAuth 授权与 `/mcp` 调用两道边界白名单不一致导致单边 `403 Origin not allowed` 或回调拒绝；5 处分散硬编码在新增宿主时极易漂移。
- **优先级**：**P2**
- **具体优化方案**：在 `shared/` 定义单一可信默认宿主常量集，派生 `hub/mcp.py` 默认 `Origin` 与 `hub/oauth.py` 默认 `OAUTH_REDIRECT_HOSTS`，并在 `scripts/check_repository_contracts.py` 校验 `.env.example`、`compose.yml` 与 `k3s/deployment.yaml` 一致性。

#### `ISS-11` [过度耦合] 千行上帝模块、27 组双向循环与 89 模块 SCC、穿透标准库 `Queue._queue` 及 60 处跨文件 `._attr` 访问
- **文件路径与行号**：`hub/runtime.py:1-4322`（含 `:560-567`）、`agent/runner.py:1-3090`、`hub/collaboration/rooms.py:1-2772`、`hub/collaboration/service.py:1-2159`、`hub/config.py:77`、`hub/oidc.py:32`、`hub/principal.py:71`、`hub/room_scope.py:352,583-584`、`hub/device_delivery.py:56,69,74,82`、`hub/collaboration/room_runtime.py:90,144,254,256,384,697`、`hub/token_dashboard.py:658-682`
- **代码证据**：
  ```python
  # hub/runtime.py:560-567 —— 直接遍历并原地覆写 Python 标准库 asyncio.Queue 私有双端队列 _queue
  q._queue[idx] = message
  # hub/device_delivery.py:69 & hub/token_dashboard.py:658,682 —— 跨模块调用私有方法并动态增删私有属性
  await self.runtime._prepare_for_send(...)
  runtime._token_dashboard_service = service  # stop 时再 delattr
  ```
- **触发条件**：升级 Python 标准库版本、演进投递/鉴权状态机、对 `hub/` 子模块做独立单元测试或扩展静态类型门禁。
- **实际影响**：全仓存在 **27 组双向循环依赖**，`hub/` 下 **89 个模块结成单一强连通分量（SCC）**（靠百余处函数内惰性 `import` 掩盖），连基础鉴权模型 `principal` 也被卷入 `Runtime` 循环；**60 处跨文件访问 `._attr`** 与穿透 `asyncio.Queue._queue` 破坏封装契约。
- **优先级**：**P2**
- **具体优化方案**：用显式去重有界队列（`OrderedDict` + `asyncio.Event`）替换 `q._queue` 私有字段穿透；在 `Runtime.__init__` 显式声明子服务生命周期属性并将跨模块调用收敛为公开方法；下沉纯数据模型打断 `config ↔ oidc` 与 `principal → room_scope → runtime` 循环链。

#### `ISS-12` [不必要的抽象（模块层）] 单调用方微模块双向成环、重复建索引碎片与错放 `shared/` 的单端模板
- **文件路径与行号**：`agent/project_index.py:1-137`、`agent/context.py:41-45`、`hub/auth_schema.py:1-17`、`hub/store.py:448-449,938-940`、`hub/dispatch_plan.py:1-10`、`shared/workflow_templates.py:1-38`、`hub/workflows.py:18`、`hub/collaboration/room_event_migration.py:1-93`、`hub/collaboration/room_message_migration.py:1-118`
- **代码证据**：
  ```python
  # agent/project_index.py 仅被 agent/context.py 调用，却反向导入 context 常量与函数形成双向环
  from agent.context import DOCUMENTS, SKILL_ROOTS, DOC_INDEX_MAX_BYTES, index_document
  # hub/auth_schema.py:16 仅被 hub/store.py:938 调用一次，且重复执行 store.py:449 已有的同句建索引 SQL
  db.execute("CREATE INDEX IF NOT EXISTS login_attempts_ip_time ON login_attempts(ip,at)")
  ```
- **触发条件**：维护 Agent 项目索引、排查主库/协作库 Schema 迁移或修改工作流模板。
- **实际影响**：拆出的微模块并未解耦依赖，反而制造 `context ↔ project_index` 循环导入与重复 DDL；10 行的 `hub/dispatch_plan.py`（仅定义 `DeferredCall`）与仅 `hub/workflows.py` 使用却放入跨端 `shared/` 的 `workflow_templates.py`（38 行）增加无收益的跨目录跳转。
- **优先级**：**P2**
- **具体优化方案**：将 `agent/project_index.py` 内聚回 `agent/context.py`；将 `hub/auth_schema.py` 并入 `hub/store.py` 并删除重复索引语句；将 `shared/workflow_templates.py` 迁回 `hub/workflows.py`；将 `DeferredCall` 与单调用方协作迁移碎片归并至对应宿主模块。

#### `ISS-13` [不必要的抽象（垫片与路由挂载层）] `import *` 垫片吞掉非零退出码，协作室拆 4 挂载器致双调 `store.run` 争锁与漏响应头
- **文件路径与行号**：`agent/relay_browser_host.py:1-6`、`agent/relay_control.py:1-6`、`scripts/relay_browser_host.py:1-6`、`scripts/relay_control.py:1-6`、`scripts/codepier_browser_host.py:1-13`、`scripts/codepier_control.py:1-13`、`agent/service_watchdog.py:90-102`、`hub/collaboration/api.py:9-20`、`hub/collaboration/room_api.py:20-35`、`hub/collaboration/room_runtime_api.py:8-92`
- **代码证据**：
  ```python
  # agent/relay_control.py:1-6（其余 3 个 relay_*.py 及 service_watchdog.py:102 同构）—— 漏 raise SystemExit
  from agent.codepier_control import *
  if __name__ == "__main__":
      main()  # codepier_control.main() 失败返回 int 1 被直接丢弃，进程退出码恒为 0！
  ```
  ```python
  # hub/collaboration/room_runtime_api.py:50-55 —— 单个请求连续两次进入单线程 StoreWorker
  principal = await store.run(auth.admin, request, request.method != "GET")
  return await store.run(service.room_runtime.send, {**body, "conversation_id": conversation_id}, principal)
  ```
- **触发条件**：通过 `relay_*` 兼容入口或 `python -m agent.service_watchdog` 执行失败返回 `1`；或前端高频请求 `room_runtime_api.py` 下的聊天发送、工作控制与渠道接入接口。
- **实际影响**：命令行控制失败时进程仍以退出码 `0` 假成功退出，误导外部管理脚本；`/api/collaboration` 被拆分为 4 个挂载函数，`room_runtime_api.py` 前 8 个端点把鉴权与业务拆成两次 `await store.run(...)`，在单写线程上翻倍排队争锁且制造 TOCTOU 窗口，后 7 个端点漏设 `Cache-Control: private, no-store`。
- **优先级**：**P2**
- **具体优化方案**：为 4 个 `relay_*.py:6` 与 `agent/service_watchdog.py:102` 补齐 `raise SystemExit(main())`，将 `scripts/codepier_*.py` 的 `import *` 改为显式导入；将 92 行的 `room_runtime_api.py` 并入 `room_api.py`，合并为单次 `store.run` 鉴权+执行并统一设置 `Cache-Control: private, no-store`。

#### `ISS-14` [重复冗余逻辑] `ToolRouter` 绕过 Schema 缓存且 `"full"=="core"` 冗余占槽，11 处同步端点双鉴权，JSON 指纹 / SSE / `shield` 样板重复
- **文件路径与行号**：`hub/tool_router.py:105-165`、`shared/contracts.py:1811-1875`、`hub/access.py:116-125,206-210`、`hub/access_profiles.py:348-358,426-431`、`hub/iam_api.py:144-152`、`hub/oidc.py:1404-1410`、`hub/roles.py:462-470,513-518`、`hub/api/file_import_settings.py:21-34`、`hub/api/settings.py:318-326`、`hub/collaboration/room_api.py:318-355`、`agent/async_work.py:13-24`、`web/token-usage.js:108-113`
- **代码证据**：
  ```python
  # shared/contracts.py:1818, 1839 —— profile 仅允许 core/full/coding，:1839 条件恒真且 full 与 core 100% 等价
  if profile not in {"core", "full", "coding"}: raise ValueError("Unknown MCP tool profile")
  if profile in {"full", "coding", "core"}: ...
  # hub/roles.py:462-470 等 7 文件 11 处 @database_endpoint —— 事务外与事务内连续跑两次完全相同的 auth 查库
  auth.instance(request, True)
  with store.transaction():
      principal = auth.instance(request, True)
  ```
- **触发条件**：每次 `tools/list` 目录请求；保存设置、角色、访问配置、OIDC、空间或文件导入策略；SSE 推送与异步取消收尾。
- **实际影响**：`ToolRouter.definitions` 每次只按默认参数调 `tool_definitions()` 再在外部二次裁剪 `project_output_schemas`，使 `_catalog_template` 的 `lean`/`none` 缓存槽闲置而 `"full"` 重复占用 6/18 缓存槽；11 处管理写接口在单线程 `StoreWorker` 内无意义重复鉴权两次；6+ 处 canonical JSON + SHA-256（`allow_nan` 不一致）、`HintStream`/`RoomHintStream` SSE 包装、11 处手写 `while not task.done(): await asyncio.shield(task)` 与前端 `escape` 散落重复。
- **优先级**：**P2**
- **具体优化方案**：`ToolRouter.definitions` 先调 `_native_tool_names` 校验全量保留名冲突，再向 `tool_definitions(profile, authorization, schemas)` 透传参数命中缓存（将 `"full"` 归一到 `"core"` 并删 `:1839` 恒真分支）；合并 11 处 `@database_endpoint` 为单次事务内鉴权；在 `shared/util.py` 收敛 `canonical_json`/`json_sha256`，复用 `finish_cleanup`、SSE 响应头辅助函数与前端 `CP.ui.esc`。

#### `ISS-15` [死代码 / 黑洞表] `Chatroom.read` 尾部 ~50 行不可达、SQLite 表 `dot_message_coverage` 只写不读及 `chat_worker` 恒假分支
- **文件路径与行号**：`hub/collaboration/chatroom.py:934-945,1032-1135`、`hub/collaboration/message_handling.py:41-49,170-182`、`agent/chat_worker.py:31-32,1124`
- **代码证据**：
  ```python
  # hub/collaboration/chatroom.py:1032-1088 已覆盖 timeline/thread/search/members/rooms/message_status/changes/_legacy_changes 全部返回
  if kind in {"timeline", "thread", "search"}:
      return self.c.conversations.read(args, principal, room)
  ...
  # :1089-1135 再次判断 if kind == "thread" / "search" 并调用 self.latest(room) / self.read_sequence(...) —— 100% 不可达
  ```
  ```python
  # hub/collaboration/message_handling.py:171 —— 持续 INSERT，但全仓生产代码 0 SELECT
  "INSERT OR IGNORE INTO dot_message_coverage VALUES (?,?,?,?,?,?,?,?)"
  ```
- **触发条件**：协作消息读取、覆盖状态更新落库，以及 Agent `chat_worker` 事件写入。
- **实际影响**：`Chatroom.read` 残留近 50 行死代码与 2 个零调用方法（`latest`、`read_sequence`）造成维护误导；`dot_message_coverage` 表与 `dot_coverage_reply` 索引产生无消费方的 SQLite 写放大；`agent/chat_worker.py:1124` 在 `bounded_event` 已截断至 `MAX_EVENT = 256 KiB` 后判断 `len(data) > MAX_LINE`（`64 MiB`）恒为 `False`。
- **优先级**：**P2**
- **具体优化方案**：删除 `hub/collaboration/chatroom.py:934-945,1089-1135` 不可达代码与 `agent/chat_worker.py:1124` 恒假判断；若近期无审计查询消费计划，停止写入 `dot_message_coverage`（或补齐诊断读取视图）。

#### `ISS-16` [配置未生效 / 运维缺陷] Agent `LspPool` 未接入 `config.json` 且切 Loop 孤儿化子进程，k3s 备份文档命令漏必填参数必败
- **文件路径与行号**：`agent/lsp_navigation.py:771-798,888-895`、`agent/integration_config.py:107-155`、`deploy/k3s/deployment.yaml:20`、`hub/__main__.py:86-88`
- **代码证据**：
  ```python
  # agent/lsp_navigation.py:793-798 —— 事件循环切换时直接清空字典，未关闭底层语言服务器子进程
  if self._loop is not loop:
      self._entries.clear()
  ```
  ```yaml
  # deploy/k3s/deployment.yaml:20 —— 漏传 hub/__main__.py:88 required=True 的 --keys-output
  # Back up before upgrading: kubectl -n codepier exec deploy/codepier-hub -- python -m hub backup --output /tmp/backup.zip
  ```
- **触发条件**：系统服务托管模式下尝试通过 `config.json` 调整或热更新 Agent LSP 池容量/TTL；事件循环重建触发 `_sync_loop`；运维按 k3s 模板注释执行升级前备份。
- **实际影响**：托管 Agent 无法通过配置文件持久化或热重载 LSP 池参数；Loop 切换时已启动的 Pyright/TSServer 等子进程泄漏为孤儿进程；按 k3s 注释执行备份 100% 报 `the following arguments are required: --keys-output`（退出码 `2`）。
- **优先级**：**P2**
- **具体优化方案**：在 `agent/integration_config.py` 允许配置可选 LSP 池容量与空闲 TTL 并支持热更新；`_sync_loop` 清空旧条目时终止底层子进程；为 `deploy/k3s/deployment.yaml:20` 备份命令补齐 `--keys-output`。

---

### 2.4 P3 低优先级问题（均已核实排除动态反射、钩子与外部入口）

#### `ISS-17` [死代码] 前端格式化函数 `timeOnly(n)`
- **文件路径与行号**：`web/call-log.js:151-155`
- **代码证据**：`function timeOnly(n) { if (!n) return '未记录'; ... }`
- **触发条件**：调用日志页脚本加载（渲染已统一改用 `:146` `timeShort(n)`，全仓检索 `timeOnly` 仅声明处 1 处命中）。
- **实际影响**：增加前端包体积与同名时间格式化认知噪音。
- **优先级**：**P3**
- **具体优化方案**：删除 `web/call-log.js:151-155` 并重新生成静态资源哈希清单。

#### `ISS-18` [死代码] `Store.async_security_epoch`
- **文件路径与行号**：`hub/store.py:990-991`
- **代码证据**：`async def async_security_epoch(self) -> int: return await self.run_read(self.security_epoch)`
- **触发条件**：全仓 0 调用（生产统一使用 `:996` `async_epoch_stamp`）。
- **实际影响**：核心存储类暴露无消费者的冗余异步接口。
- **优先级**：**P3**
- **具体优化方案**：删除 `hub/store.py:990-991`。

#### `ISS-19` [死代码] `rewrite_task_ciphertext`
- **文件路径与行号**：`hub/operation_history.py:380-382`
- **代码证据**：`def rewrite_task_ciphertext(store, task_id, column, encrypted):`
- **触发条件**：生产代码 0 调用（密钥轮换 `hub/keyring.py` 已改用批量 `task_rekey_session`），仅 `tests/test_history_*.py` 2 个单测调用。
- **实际影响**：保留逐条重建 SQLite 触发器的低效废弃辅助函数。
- **优先级**：**P3**
- **具体优化方案**：将 2 个单测迁移至 `task_rekey_session` 后删除该函数。

#### `ISS-20` [死代码] `visible_actions`
- **文件路径与行号**：`hub/unified_tools.py:72-73`
- **代码证据**：`def visible_actions(tool, scopes=None, *, collaboration=False): return list(directory(scopes, collaboration=collaboration)[tool])`
- **触发条件**：生产代码 0 调用，仅 `tests/test_unified_tools.py:250-259` 调用。
- **实际影响**：保留无生产用途的一行包装函数。
- **优先级**：**P3**
- **具体优化方案**：单测直接断言 `list(ut.directory(...)[tool])`，删除 `visible_actions`。

#### `ISS-21` [死代码] `ChannelMediaRouter.cache_usage`
- **文件路径与行号**：`hub/collaboration/channel_media_router.py:110-116`
- **代码证据**：`def cache_usage(self, exclude_chunks=None, *, space_id=None, owner_user_id=None):`
- **触发条件**：全仓（含测试）0 调用，调用方均直接访问 `media_cache.available` / `usage`。
- **实际影响**：残留无调用透传包装。
- **优先级**：**P3**
- **具体优化方案**：删除 `hub/collaboration/channel_media_router.py:110-116`。

#### `ISS-22` [死代码] `ConnectionEvidence.reset_dedup`
- **文件路径与行号**：`hub/connection_evidence.py:75-78`
- **代码证据**：`def reset_dedup(self): with self._lru_lock: self._observed_lru.clear()`
- **触发条件**：生产代码 0 调用，仅 `tests/test_b11_b17_bg_gateway.py` 调用。
- **实际影响**：生产类混入纯测试重置钩子。
- **优先级**：**P3**
- **具体优化方案**：单测改为构造新实例或推进注入时钟，删除 `reset_dedup`。

#### `ISS-23` [死代码] 单模式脱敏类 `SecretOutput`
- **文件路径与行号**：`shared/secret_output.py:6-35`
- **代码证据**：`class SecretOutput: def __init__(self, secret: str = ""):`
- **触发条件**：生产代码已全面切换至 `:37` Aho-Corasick 多模式 `SecretOutputs`，仅 `tests/test_ssh.py:18,83` 引用旧类。
- **实际影响**：维护两套流式跨边界脱敏状态机，且 `test_ssh.py` 未覆盖生产实际运行的 `SecretOutputs`。
- **优先级**：**P3**
- **具体优化方案**：将 `tests/test_ssh.py` 迁移至 `SecretOutputs([secret])`，删除 `SecretOutput`。

#### `ISS-24` [死代码] 同步方法 `Agent.cancel_pending_call`
- **文件路径与行号**：`agent/runner.py:810-816`
- **代码证据**：`def cancel_pending_call(self, id): ...; return self._fence_pending_call(id)`
- **触发条件**：生产取消链路统一走异步 `:796` `_cancel_pending_frame(id)`，仅 3 个单测调用该同步旧入口。
- **实际影响**：保留在事件循环线程直接同步查库的过时入口。
- **优先级**：**P3**
- **具体优化方案**：单测迁移至 `await agent._cancel_pending_frame(id)`，删除 `cancel_pending_call`。

#### `ISS-25` [死代码] `LspPool.resident_count` 与 `LspPool.evict_idle`
- **文件路径与行号**：`agent/lsp_navigation.py:808-813,871-877`
- **代码证据**：`def resident_count(self) -> int:` / `async def evict_idle(self) -> int:`
- **触发条件**：生产空闲回收由 `_reaper_loop` 与 `evict_for_memory_pressure` 驱动；`evict_idle` 全仓 0 调用，`resident_count` 仅单测断言。
- **实际影响**：池管理类残留未接线方法。
- **优先级**：**P3**
- **具体优化方案**：删除 `evict_idle`；`resident_count` 接入 Agent 诊断指标或迁入测试辅助函数。

#### `ISS-26` [死代码] 失效压测脚本 `scripts/benchmark_five_directories.py`
- **文件路径与行号**：`scripts/benchmark_five_directories.py:1-217`
- **代码证据**：`await bench.rpc(client, "project_query", {"operation": "tree", ...})`
- **触发条件**：全仓（含 CI、`docs/VERIFICATION_TOOLS.md` 与测试）0 引用；手动运行即因调用已移除工具 `project_query` 报 `TOOL_REMOVED`。
- **实际影响**：增加源码清单与复杂度基线维护负担。
- **优先级**：**P3**
- **具体优化方案**：删除该脚本并运行 `python3 scripts/update_source_inventory.py` 刷新公开清单。

#### `ISS-27` [死代码] `scripts/mcp_stdio_bridge.py` 内的 `REMOTE_TOOLS`
- **文件路径与行号**：`scripts/mcp_stdio_bridge.py:32`
- **代码证据**：`REMOTE_TOOLS = {name for name, tool in TOOLS.items() if not tool.local}`
- **触发条件**：网桥运行期 0 引用，仅 `tests/test_agentdock_bridge.py:11` 与 `tests/test_reliability.py:96` 导入做同义集合断言。
- **实际影响**：保留无运行期作用的导出常量。
- **优先级**：**P3**
- **具体优化方案**：删除 `REMOTE_TOOLS` 并清理对应两处单测同义断言。

#### `ISS-28` [死代码] `HubIntegrations.capture_output`
- **文件路径与行号**：`hub/integrations.py:385-415`
- **代码证据**：`def capture_output(self, trace, result):`
- **触发条件**：生产链路已改为异步/离线程采集并落库，该方法在生产代码 0 调用，仅 `tests/test_b1_b14_b3_b8_b16_hub_core.py:343` 直接单测。
- **实际影响**：保留无生产调用方的旧同步估算入口。
- **优先级**：**P3**
- **具体优化方案**：核对生产实际调用的输出统计函数覆盖率后移除孤立方法及对应过时单测桩。

#### `ISS-29` [死代码 / 同名异构] `shared/context_estimate.py` 中的 `reference_cost`
- **文件路径与行号**：`shared/context_estimate.py:346-380`
- **代码证据**：`def reference_cost(value, *, input_rate, cached_input_rate, output_rate, cache_percent=95):`
- **触发条件**：生产代码（`hub/context_usage.py:6`、`hub/mcp_usage.py:9`、`shared/token_estimate.py:19`）全部导入 `shared.token_cost.reference_cost`，`context_estimate.reference_cost` 仅 `tests/test_context_estimate.py:7` 调用。
- **实际影响**：与生产权威计费函数 `shared.token_cost.reference_cost` 同名但参数签名和计价粒度不同，极易引发误导入。
- **优先级**：**P3**
- **具体优化方案**：若仅供场景估算单测使用，迁入 `tests/test_context_estimate.py`（或改名消除同名遮蔽）。

---

## 3. 待验证风险（仓内生产零引用，须结合线上数据核实后处置）

#### `RSK-01` [待验证 / 旧版协作接口退役] `hub/collaboration/api.py` 12 个 GET + 66 个 POST 操作及 `route-prepare` 生产前端零引用
- **文件路径与行号**：`hub/collaboration/api.py:20-90,151-467`、`hub/collaboration/room_api.py:69-71`
- **代码证据**：
  ```python
  # hub/collaboration/api.py —— 除 :9-19 make_router 与 :93-149 GET /attachments/{id}[/content] 外：
  # 12 个旧 GET：/status, /room-scopes, /weixin, /telegram, /conversations, "", /relay-sync, /relay-hints,
  #             /message-receipt, /diagnostics, /dot-recovery, /delegation-connection, /delegation-inbox
  # POST /{operation} (:380-467)：weixin-qr-start/status (2) + handlers 字典 (64) = 66 个旧写操作
  ```
- **触发条件**：外部自建自动化脚本或未升级客户端直接请求旧版 `/api/collaboration/*` 接口（仓内现役前端 `web/collaboration-room/*` 已全部切换至 `/api/collaboration/rooms/*`、`/work/*`、`/connections*`、`/*-connect-flows/*`，旧版前端静态文件已在 `hub/static_assets.py:25-40` 返回 `404`）。
- **实际影响**：79 个旧读写端点继续暴露在面板会话路由上，既扩大受保护接口攻击面，又使 `hub/collaboration/` 下大量历史兼容服务模块无法剪枝。**注意**：同文件内的 `make_router`、`GET /attachments/{identifier}[/content]` 以及 `room_api.py:50-67` 的 `POST /connections/rebind-batch`（被 `web/collaboration-room/index.mjs:161` 动态拼接调用）仍为现役核心接口，严禁误删。
- **优先级**：**P1（待验证）**
- **具体优化方案**：先为上述旧路由挂载访问计数日志、`Deprecation: true` 响应头及默认关闭特性开关 `CODEPIER_LEGACY_COLLABORATION_API=false`；核实线上反向代理与审计日志连续一个迭代周期 0 外部调用后，将 `attachments` 路由收口至 `room_attachments.py` 并正式下线旧路由与旧文档引用（`docs/DOT_COLLABORATION.md:30`、`docs/DOT_RELAY_EXPERIENCE.md:59`）。

#### `RSK-02` [待验证 / 存量客户端兼容窗口] 4 个 `relay_*` 旧品牌垫片与旧 MCP Profile（`core`/`full`/`coding`）及网桥默认 `core`
- **文件路径与行号**：`agent/relay_browser_host.py:1-6`、`agent/relay_control.py:1-6`、`scripts/relay_browser_host.py:1-6`、`scripts/relay_control.py:1-6`、`shared/brand_browser.py:198-205`、`hub/mcp.py:178-186`、`scripts/mcp_stdio_bridge.py:398-400`
- **代码证据**：
  ```python
  # scripts/mcp_stdio_bridge.py:400 —— HTTP /mcp 已默认 unified，但本地 stdio 网桥仍默认拉取旧 core 目录
  profile = os.environ.get("CODEPIER_MCP_PROFILE", "core")
  ```
- **触发条件**：存量 Agent 宿主机上的 Chrome `NativeMessagingHosts` 清单尚未经 `brand_browser.py` 刷新重写；或本地 MCP 客户端未显式设置 `CODEPIER_MCP_PROFILE=unified`。
- **实际影响**：`shared/` 需同时维护逾 220 KB 新旧双轨工具契约与 Schema 投影逻辑，且默认 stdio 用户每次会话挂载 `core` 多工具目录消耗显著更高的上下文 Token。
- **优先级**：**P2（待验证）**
- **具体优化方案**：统计线上 `mcp_activity` 表各 `profile` 真实调用占比，在修复 `ISS-02` 后将 `mcp_stdio_bridge.py` 默认值平滑切换为 `unified`；结合存量 Agent 版本上报确认旧版宿主机均已完成浏览器清单迁移后，移除 4 个 `relay_*` 垫片并刷新 `shared/source_inventory.json`。

#### `RSK-03` [待验证 / 孤立运维诊断接口] `GET /api/system/health` 与 `GET /api/system/collaboration-telemetry` 仓内生产零调用
- **文件路径与行号**：`hub/api/system.py:82-93,160-170`
- **代码证据**：
  ```python
  # hub/api/system.py:82, 160 —— 容器与 k3s 探针使用 /livez /readyz /healthz；此两处需实例管理员登录态且前端 0 引用
  @router.get("/api/system/health")
  @router.get("/api/system/collaboration-telemetry")
  ```
- **触发条件**：外部带管理员 Cookie 的运维巡检脚本主动拉取（仓内 `web/`、`scripts/`、`deploy/` 均无调用）。
- **实际影响**：若无外部巡检消费则属孤立路由；若设计初衷为暴露实例写错误计数与微信积压状态，因未接入面板导致管理员在界面上不可见。
- **优先级**：**P3（待验证）**
- **具体优化方案**：核实线上是否有外部运维脚本调用；若需保留观测价值，将其接入设置中心“面板更新与诊断”折叠区展示，否则予以清理。

---

## 4. 第一性原理保留项与已排除伪阳性说明

为避免机械变量化与误删有效代码，以下经全链路核查确认**合理且应予保留**：

1. **领域安全、协议边界与容量常量（不应外置为配置）**：
   - `hub/http.py:18,46-53` `HSTS_SECONDS`、`BodyLimit`（常规 API `6 MiB`、上传 `64 MiB`）、`hub/__main__.py:53` 与 `agent/runner.py:642` WebSocket 帧上限 `16 MiB`、`hub/collaboration/api.py:111` 附件并发解码信号量 `4`（匹配容器 `1 GiB` 内存预算）；
   - `agent/chat_worker.py:31-65` 子进程管道行上限 `MAX_LINE = 64 MiB` 仅作用于本机 `pi` CLI `stdout` 读取，紧邻 `bounded_event` 已截断至 `MAX_EVENT = 256 KiB` 并按 `64 KiB` 分块同步，不会触发 WebSocket `1009` 超限断连。
2. **解耦循环依赖与单写串行化的窄契约抽象（非过度抽象）**：
   - `hub/db_worker.py`（26 处路由复用，统一约束读写事务边界）、`hub/api/context.py`（10 个 API Router 依赖注入）、`hub/operation_categories.py`（5 处共享 SQLite 索引谓词）、`shared/instance_lock.py`（9 处跨平台进程锁）及 `shared/*_contracts.py`（保障 Agent 端零 FastAPI/Web 依赖）。
3. **经核实存活的动态入口、平台钩子、测试契约与安全锁依赖（非死代码 / 非无用依赖）**：
   - **Windows 服务守护入口**：`agent/service_watchdog.py` 由 `scripts/install_agent.py:492` 生成的计划任务拉起，且被 `deploy/start-agent.cmd:5`、`scripts/agent_lifecycle.py:548` 等 6 处作为运行时完整性门禁校验；
   - **自举脚本同步副本**：`scripts/sync_standalone_helpers.py --check` 校验的独立安装器副本（保障脱离包环境自举运行）；
   - **框架与 CI 动态入口**：`agent/skills.py:105` `MetadataLoader.construct_mapping`（PyYAML 构造钩子）、`shared/runtime_qualification.py:44` `pinned_base_image`（`.github/workflows/ci.yml:92` 调用）、`agent/workspace_probe.py:426` 子进程入口；
   - **显式文档化的测试响应契约**：`shared/collaboration_contracts.py:340` `JoinResult` 在 `docs/VERIFICATION_TOOLS.md:33` 明文规定保留用于 HTTP/服务测试校验实际返回结构，零生产序列化开销；
   - **安全传递依赖锁**：`requirements/requirements.in` 中 `opentelemetry-api==1.45.2`（CVE 安全版本锁）、`requirements-bridge.in` 中 `pydantic` 及前端 `web/chat/*.js`（`build-room.mjs` 编译源）。

---

## 5. 未验证部分与边界声明

1. **生产环境真实流量与历史数据库状态**：本次为静态全仓与契约级深度扫描，未读取线上反向代理访问日志、生产 `audit` / `mcp_activity` 表及 `meta` 表中历史 `mcp_access_defaults:*` 记录；`RSK-01`–`RSK-03` 的最终删除时间表须以线上零外部调用统计为准。
2. **外部第三方网关与跨操作系统真机环境**：微信 iLink 网关、Telegram Bot API、外部 OIDC IdP、Windows 真机计划任务 / `pywinpty` ConPTY 及 macOS `launchd` 服务迁移已完整核查源码实现与测试夹具，未发起公网真实请求或跨系统宿主机实机安装压测。
3. **网络存储（SMB/NFS）延迟实测与浏览器动态覆盖率**：`ISS-03` 消除单次写入 4–5 次重复读盘及 `ISS-05` 透传 `CODEPIER_SQLITE_SYNCHRONOUS=NORMAL` 在网络挂载盘上的 P99 收益待实机基准量化；`web/app.js` 与 `web/call-log.js` 内部针对远古历史回执字段的兼容渲染分支可进一步结合 Playwright V8 Coverage 做运行期精简。
