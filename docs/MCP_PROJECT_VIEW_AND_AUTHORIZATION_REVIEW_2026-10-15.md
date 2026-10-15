# 项目最小视图与授权语义：设计评审及实施状态

日期：2026-10-15

> **第 1—7 节保留前两轮历史状态，不是当前阻塞清单。最新结论见第 8 节及配套 ACCEPTANCE 文档。**

**当前状态：P0/P1 本地实现完成，已知失败已清零；发布部署和真实客户端上线验收未执行。**

## 1. 基线和工作边界

- 原设计：`docs/MCP_PROJECT_VIEW_AND_AUTHORIZATION_DESIGN_2026-10-15.md`，原始 SHA-256：`218aa7525300170160fed4d53f12f649970b8211e7de002e628dc15e3d69c8ce`。本工作树保留原文，没有将未完成事项改写成已实现。
- 代码基线：`c6ea62dc8ef9b7d69060b35c83f9672a311dd77a`。
- 工作流：`8575c52ca9b446a2b42811a1f0fe221f`。
- 隔离工作树：`92c77a5c00534d469fb6944768a8e3a3`，标签 `project-view-authorization-20261015`。
- 已存在另一协作优化工作流，本轮没有覆盖其工作树。本轮代码未合入主工作区、未提交或推送、未部署，未重启线上 Hub/Agent。
- 初始 baseline_ref 为 `362a4325e52b4d8bbb9dca38e004b3f7`，其文件快照命中 8 MiB 上限，不能作为完整仓库备份；已记录的 Git 基线才是已跟踪代码的完整比较起点。

## 2. 设计是否足够

总体方向正确，不必推翻重写：明确项目直接打开、默认最小索引、按动作区分权限与可执行状态、展示层不改变实际权限、保留原始回执与内容，这些目标可以实施。

但草案还不足以直接作为“全部完成、可以发布”的证据。实际代码评审确认必须补齐以下工程细节。

### 2.1 严格区分三层状态和实际执行证据

CodePier 的当前授权、执行节点的时间点观察、宿主平台的审批决定必须分别表示。`unknown` 或 `not_observed` 既不等于允许，也不等于拒绝或必须再次确认。查询不产生文件写入/命令执行成功证据。托管任务仍需原任务授权及有效 lease；连接配置不能代替它。

文件写入不能被 shell、allow_tasks、computer 或 Codex 禁用状态误伤。动作诊断应直接复用 `FileEngine.root(..., write=True)`、`shell_denial`、Hub 的角色/资源授权、房间上限和暂停控制等既有检查，而不是另建一套宽松权限判断。它只能覆盖项目默认条件，精确路径、SHA、命令内容、目标操作系统权限及任务 lease 仍由真实操作校验。

### 2.2 最小上下文不能把规则一并删掉

原始 `agent/context.py` 的根文档清单不含 MEMORY.md，默认还调用 execution_info。最小模式需要保留 AGENTS.md、MEMORY.md、CLAUDE.md 的路径、SHA 和可读状态；预算很小也不能静默遗漏这些规则。没有建立索引、确认不存在、被保护路径规则拒绝，三者必须区分。正文按需读取，文档不能成为授权来源。

### 2.3 明确新旧 Agent 协商和上下文失效

只添加可选参数仍可能被旧 Agent 的 extra=forbid 拒绝。新字段应由认证握手的显式能力标记协商；仅修改实际发送的参数副本，不改变原请求、幂等指纹或回执。旧 Agent 未观察到的状态应降级为 unknown，不应先发送未知参数再自动重试。上下文复用必须考虑视图版本、detail、workspace、规则 SHA 及授权版本。

### 2.4 所有原生 MCP 出口需同一表示层

当前 `shared/mcp_presentation.py` 仍依赖字典形状递归处理，普通项目结果仍保留 device_id。最终实现需要按工具、操作、结果类型选择字段白名单，覆盖直接响应、get/list/wait/trace、标准 Tasks 终态、rd://projects、协作中的项目摘要及附加 metadata。设备管理、项目映射和 VPS 目标选择需要的真实标识必须保持可用。

原始文件正文、BOM/SHA、diff、stdout/stderr、显式技能资源、原生媒体和第三方 MCP 数据应保真，不能靠全局正则删改内容。内部持久回执和面板数据不得被展示投影覆盖。**这部分改造本轮未写入。**

### 2.5 双版本 schema 和提示长度也是验收条件

保留旧 readiness 返回形状时，新的严格动作 schema 必须放在独立的类型分支；不能把新 checks 的 required 字段施加到旧 checks 上。发现提示的新增内容也必须满足原来的 4000 字符预算。实际集成测试已发现本轮实现存在这两项回归，尚未修正，详见第 5 节。

### 2.6 修正文档中的测试依据

`tests/test_resource_readiness.py` 检查的是 CI 运行资源准备情况，不是本功能的动作授权诊断，不能拿它证明 file_write/command_exec 的授权语义正确。本轮新增 `tests/test_project_view_foundation.py` 专门覆盖相关基础行为。

## 3. 已实际写入的实现

### 3.1 定向项目发现

`shared/contracts.py` 新增严格 ProjectsList 参数；`hub/project_views.py` 在可见性和项目读取授权之后进行标准化搜索、稳定排序、计数与分页，并记录连接观察来源与时间。`hub/runtime.py` 接入该分支。提示不再要求明确目标也先列出全部项目。

### 3.2 最小 open 与规则索引

`shared/coding_contracts.py` 新增 `context_detail=minimal|documents`，默认 minimal。`agent/project_index.py` 实现有界的项目本地规则、文档、技能及目录索引，不自动探测执行环境。`agent/context.py` 增加 MEMORY.md，并让明确的 open 文档模式也不调用 execution_info。旧内部 project_context 的既有预览行为暂时保留。

`agent/coding_reviews.py` 将视图版本、detail、workspace、规则索引和授权修订加入上下文指纹，输出视图版本与观察时间。

### 3.3 动作 readiness 基础

`shared/project_view_contracts.py` 定义动作类型和状态结构；`agent/action_readiness.py` 复用本地文件根目录、shell 与暂停检查；`hub/project_views.py` 在直接读取和轮询观察时重新检查当前 Hub 作用域、角色/项目权限及房间限制。

`shared/integration_contracts.py`、`agent/integrations.py`、`agent/runner.py`、`hub/runtime.py` 接入该逻辑。平台审批保持 not_observed，operation_evidence 为 null。旧/离线 Agent 使用明确的兼容性或连接状态，不编造允许。

### 3.4 协商基础

`shared/tool_protocol.py`、`hub/runtime.py` 与 `agent/runner.py` 增加 project_view_protocol 标记及可信授权修订传递。发往旧 Agent 的新增参数只在发送副本中去除。

### 3.5 专项测试

新增 `tests/test_project_view_foundation.py`：根规则索引、最小/正文视图、符号链接保护、上下文失效、文件动作与命令/电脑权限独立、既有只读与暂停门禁、严格输入验证、拒绝调用方伪造审批字段、撤权重查、映射变化、授权后搜索分页、协商不修改原始参数等。

## 4. 已执行验证

| 验证 | 真实结果 | 证据 |
| --- | --- | --- |
| 原上下文/快照相关测试 | 23 passed | 操作 `23c76e86400842af888588692c295770`；`.work/project-view/first-tests.log` |
| 新基础测试 + 原上下文/快照测试 | 46 passed | 操作 `85c1eee27b9d4261a7b21b4756acdfce`；`.work/project-view/foundation-tests.log` |
| git diff --check、源码语法编译、动作输入解析及全部广告输出 schema 结构检查 | 通过 | 操作 `155bac5e1fc04e93a6180cc0f299b13d` |
| coding integration + 现有 MCP presentation 测试 | **17 passed, 2 failed** | 操作 `1def17a065a843b8835c913288223e66`；`.work/project-view/integration-tests.log` |

46 项通过包含首轮 23 项，不应重复累计。Schema 结构合法不代表真实返回内容符合 schema；后续真实 Hub/Agent 集成测试恰好发现了内容校验问题。

## 5. 已确认的未解决回归

1. `test_profile_old_transport_and_default_full_remain_compatible`：initialize instructions 实际为 **4913 字符**，原合同要求小于 4000。需要压缩提示，保持项目定向发现、SHA/幂等、回执恢复和既有权限边界。
2. `test_real_agent_discovery_receipts_and_panel_keep_distinct_views`：旧 readiness 的 checks 项只有 name/state，但新合并 schema 错误要求 source/observed_at。必须保留旧、新两个独立且类型准确的分支，不能通过删除断言或放宽所有字段掩盖问题。

## 6. 宿主拦截和剩余范围

宿主平台两次返回“因 OpenAI 无法确定请求的安全状态，已拦截此工具调用”。第一次是表示层改造写入，第二次是上述两项回归修正。两次都没有返回 CodePier 执行操作 ID，因此不能当成已执行或已修复。没有改换入口执行被拦截的写入。

第一次拦截后已通过读取 SHA 确认 `shared/mcp_presentation.py` 和 `shared/core_output_schemas.py` 仍保持原版。成功执行的独立测试与核对不等于被拦截写入已获批准。

尚未完成：

- 上述两项回归修正及重跑。
- 基于类型的公共 metadata 投影及全部出口的一致性接入。
- 旧回执和旧 Agent 的最终公共视图适配、全量 context token/缓存失效验收。
- 协作 scope/摘要与管理视图的完整回归。
- A01–A22 逐项验收、性能前后对照、完整测试集及真实宿主新会话验收。
- 最终设计文档合订、合入、提交推送、发布部署与线上验收。

P2 可选扩展与真实 OS 隔离仍属独立范围；不能将本轮基础实现描述为已实现系统沙箱。

**交付判定：仅供后续继续实施和审阅，不应直接合入或发布。**


## 7. 本轮续作结论

上轮的初始化说明超长、readiness schema 错误合并已修复。按原操作类型选择的项目/回执投影、最小 open、旧 Agent 参数协商、上下文变化拒绝和面板历史保真均已写入；不能继续把第 3—6 节的“尚未写入表示层”当作当前事实。

最新同源码相关回归为 **407 passed / 4 failed / 0 skipped**，操作 `cf54d9cec4f941fe90d428d64bec2ea5`。四项失败对应目录字节预算、嵌套连接摘要覆盖、新策略拒绝错误码保留三项问题；其收口写入被平台拦截，仍未执行。本轮已运行 Chromium/WebKit 的相关真实浏览器场景，完成 8325 项测试的收集（不是全量执行），并量测同样本最小上下文体积减少 67.88%。

完整实现明细、失败用例、A01–A22 状态、证据文件及当前边界见 [续作验收记录](MCP_PROJECT_VIEW_AND_AUTHORIZATION_ACCEPTANCE_2026-10-15.md)。设计正文已更新到 0.2 评审修订版。当前仍不具备全部完成、合入或发布条件。


## 8. 最终本地实现与验证

此前目录预算、嵌套连接摘要、未来策略拒绝机器码三项遗留已完成。另新增复现并修复旧协作回执轮询投影遗漏；查询一致性测试改为逐次核验真实观察时间，保留所有权限与副作用断言。公开 CORE_TOOLS、ACCESS_PROFILES 和文档导航已同步，设计为 0.3。

本轮相关 31 模块 501 项通过；最终全仓快速组 5315 项通过、3013 项 slow 未选，源码和测试 SHA 在该次运行中不变。最终快速回执为 `52c974e870ab4cda8030ba147da9fadf`，日志/JUnit/源码清单位于 `.work/project-view-fast-final/`。首次快速组曾因旧时间断言失败，其记录保留在 `.work/project-view-finish-regression/`，不删除历史失败。

已通过真实隔离的新 Hub/基线旧 Agent、基线旧 Hub/新 Agent 进程兼容测试，以及当前插件通道的文件创建、读取、修改、SHA 读回和清理。25 个 Python 文件编译、Ruff、两项前端语法和差异检查通过；31 文件补丁已在干净基线应用并验证逐字节一致。

本地源码交付与完整发布是两个结论：未提交推送、未发布部署、未修改生产权限或重启服务。P2 可选扩展与真正 OS 隔离仍属独立工程。主工作区同步完成后，其回执与 SHA 核验结果追加到配套验收记录。
