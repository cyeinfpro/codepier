# CodePier MCP 工具结果保真设计（2026-11-11）

> 日期：2026-11-11（Asia/Shanghai）
>
> 范围：HEAD `37fd45129f70`（main）加工作区 8 个未提交文件（“工具结果保真优化”，见 §3）。经 Claude 连接调用统一目录 `query / act`，在项目运行环境内对合成回执做字节测量，并运行相关测试。
>
> 与前序文档的关系：`archive/MCP_CONTEXT_OPTIMIZATION_2026-11-10.md` 的 C07 把成功回执折叠到最小；工作区改动为了保真把其中一部分撤回。本文给出两者都满足的规则，并补上工作区改动没有覆盖的截断层。
>
> 状态：第 1、2 期及第 3 期流式字节计数 / SHA-256 已实现。实施差异、验收结果与边界见 §10。原评审数据保留为历史记录。

## 0. 结论

工作区改动的方向正确，其中三项应原样保留（§3.1）。但它不宜按现状提交，原因有三：

| 排名 | 问题 | 实测 | 本设计后 |
| --- | --- | --- | --- |
| 1 | 成功回执无条件保留 `continuation` / `log_ref` / `failure_context` / `recovery` | `echo ok` 精简回包 view 2：931 → 2580 字节；view 3：390 → 2039 字节 | 输出完整时与 HEAD 相同；只有输出不完整时才多出续读信息 |
| 2 | 新指纹只覆盖最后一层（规范化、响应预算）；Agent 与 Hub 读取视图的切尾仍然只有一个布尔值 | 本次评审两次 `git diff` 被切掉开头，回包没有原长、没有区间、没有续读办法 | 每一层的损失都登记，返回文本在已存尾部中的区间可寻址 |
| 3 | 预算截断后的恢复路径模型无法执行，且请求更大的页反而拿到更少 | 请求 51,192 字符的页返回 12,861；请求 19,992 返回 19,992 | 页大小单调；续读是当前连接上的一次调用 |

另有两项设计层面的调整：折叠规则集中到一个谓词（现在三处各写一份），日志/精确/透传的输出策略按工具声明而不是按回包形状判断。

实施分三期（§5）。第 1 期是纯展示层改动，建议并入当前这次提交；第 2 期新增一个向后兼容的读取参数；第 3 期涉及 Agent，可选。

## 1. 口径与边界

- **字节数**：`json.dumps(value, ensure_ascii=False)` 后 UTF-8 编码的长度，对象是 `hub.core_tools.result(...)` 的完整返回（`content` 加 `structuredContent`）。
- **合成回执**：由 `shared.runtime_results.decorate_result` 生成，ID 与哈希用等长占位。没有使用真实命令输出、凭据或项目数据。
- **“改动前”**：`git archive HEAD` 展开到临时目录后运行同一脚本，用完即删。
- **未给 token 数**：环境里没有 tokenizer，与前序文档一致。
- **不触及**：授权、幂等、存储表结构、失败与未完成状态下的诊断字段、外部 MCP 透传。

## 2. 现状：输出经过的五层

| 层 | 位置 | 行为 | 留给调用方的痕迹 |
| --- | --- | --- | --- |
| L1 Agent | `agent/runner.py` | 最终输出取末尾 2000 行，再取末尾 50 KiB | `output_truncated` 布尔值 |
| L2 Hub 存储 | `hub/operation_output.py` | 只保存末尾 131,072 字符 | 无 |
| L3 Hub 读取视图 | `hub/runtime.py`（操作视图） | `output[-limit:]`；内联等待与 `job` 默认 8000 字符，上限 51,200 | `output_truncated`、`output_chars`；后者在精简视图成功分支被删除 |
| L4 展示 | `shared/mcp_presentation.py`、`shared/unified_contracts.py` | 显式开启的规范化、相同日志副本改指针、成功字段折叠 | 工作区改动新增原始页指纹 |
| L5 响应预算 | `hub/core_tools.py::bound_batch_response` | 整个响应超过 48 KiB 时把最大的输出折半，保留头 1/5、尾 4/5 | `output_budget_limited`；工作区改动新增原始页指纹 |

两个事实决定了设计空间：

1. **完整 stdout 在任何一层都不存在。** `HUB_OUTPUT_STORAGE.md` 已写明活动输出是有界尾部快照。所以“保真”的对象只能是 Hub 已存的尾部；更早的输出要诚实地说没有，而不是给一个看似完整的指纹。
2. **默认连接的正文计两份。** view 2 的 `content` 文本和 `structuredContent` 各带一份输出，48 KiB 预算下一页实际只能放约 2 万字符。

## 3. 对工作区改动的评审

### 3.1 应保留

- **日志识别收紧。** 原来只要有 `operation_id` 就当日志，现在要求是已知执行工具，或无 `tool` 且同时带 `operation_id` 与退出码字段。未知工具及其嵌套 `operations` 不再被遍历。
- **外部 MCP 结果原样透传。** `tests/test_mcp_gateway.py` 覆盖三种连接参数与新旧两种协议。
- **扁平化时指纹与输出同源。** `_present_exec` 合并 `data` 与外层行时，展示标记只跟随实际采用的那份输出。

### 3.2 问题

**F1 · 成功回执变大 2.8–5.2 倍（P0）。** 原缺陷是“成功就删”，哪怕输出被截断；修复改成了“一律保留”。

| 展示方式 | HEAD | 工作区 | 增量 |
| --- | --- | --- | --- |
| view 2，text=json | 931 | 2580 | +1649 |
| view 2，text=compact | 919 | 2568 | +1649 |
| view 3，text=json | 390 | 2039 | +1649 |
| view 3，text=compact | 378 | 2027 | +1649 |

多出的三个对象逐字段核对：`continuation` 的 `operation_id`、`target`、`runtime_ref`、`runtime_revision` 在回包顶层和 `runtime` 里已有，`project_id` 是调用方自己传入的项目，其余是常量；`log_ref` 只有 `operation_id` 和一个常量；成功时的 `failure_context` 只表达“成功了”。全仓没有任何输入参数接收这几个对象。它们是重述，不是证据，而且按调用次数重复付费。

**F2 · 最常见的截断没有覆盖（P0）。** L1、L3 的切尾发生在指纹之前，指纹的对象是“展示前的返回页”，即已经被切过的文本。L3 切尾后精简视图只剩 `output_truncated: true`，没有原长，没有区间。

**F3 · 上游截断标记被预算层覆盖（P1）。** 预算层执行 `row["output_truncated"] = row["output_budget_limited"] = True`。实测上游是否已截断，预算后的回包字段完全相同，调用方无法判断 SHA 对应的是整段已存尾部还是其中一页。

**F4 · 恢复路径不可执行（P0）。** `output_recovery` 让调用方使用连接地址参数 `?verbosity=full&output=raw` 并加大 `output_limit`。

- 这些参数取自 `request.query_params`（`hub/mcp.py`），是连接配置，模型在对话中无法更改。
- 加大 `output_limit` 会得到不同的页，哈希必然对不上；而在默认连接上加大后还会再次触发预算截断。
- 超出 L1/L2 的部分哪里都没有，“不要为取回输出而重跑命令”对它不成立。

**F5 · 预算折半不单调（P1）。** 默认连接（view 2，text=json）下的实测：

| L3 返回页（字符） | 预算后实际返回 |
| --- | --- |
| 7,992 | 7,992 |
| 19,992 | 19,992 |
| 24,000 | 12,042 |
| 30,000 | 15,042 |
| 51,192 | 12,861 |

同一文件里读文件走的是二分精确贴合（`_fit_prefix`），日志走的是折半。折半还会把省略标记写进正文，切点可能落在一行或一个转义序列中间。

**F6 · 总结用语与实现不符（P2）。** 总结称附带“范围”，实际字段 `output_original_scope` 是固定字符串 `returned_output_before_presentation`，不是区间；保留了哪段头尾也没有报告。

**F7 · 结构（P2）。** 折叠逻辑在 `_slim`、`_present_exec`、`compact_output_text` 三处各有一份，这正是它们此前行为不一致的原因。`EXECUTION_OUTPUT_TOOLS` 是新的硬编码集合，与 `shared/core_contracts.py` 里已有的工具到类别映射重复；`unified_contracts.py` 靠函数内导入取用它，`_slim` 每层递归都会执行一次导入语句。

## 4. 设计

### 4.1 原则

1. **证据与重述分开。** 不能由同一回包其他字段或调用方已知参数推出的值是证据，永不折叠。可以推出的是重述，在“成功、终态、输出完整”时折叠。
2. **损失在发生的那一层登记，后面的层只追加。** 任何一层都不得覆盖前面一层留下的标记。
3. **只给有损变换配指纹。** 连续、未改写的一段文本用区间描述即可，重新读同一区间就是核验。规范化改写了文本，才需要原文指纹。
4. **恢复必须是当前连接上的一次调用。** 做不到时明确说明哪一部分不存在，不给执行不了的指引。
5. **策略按工具声明。** 调用方知道自己派发的是哪个工具，不需要从回包形状反推。

### 4.2 成功回执的折叠规则

定义 `output_complete`：各层都没有截断，且规范化没有改变文本。

| 字段 | 类别 | 成功、终态、输出完整 | 成功、终态、输出不完整 | 失败、超时、取消、未完成 |
| --- | --- | --- | --- | --- |
| `operation_id`、`output_seq`、`exit_code` | 证据 | 保留 | 保留 | 保留 |
| `runtime`（已折叠为引用） | 证据 | 保留 | 保留 | 保留完整 |
| `failure_context`（`category=succeeded`） | 重述 | 折叠 | 折叠 | 保留 |
| `recovery`（已确认成功） | 重述 | 折叠 | 折叠 | 保留 |
| `continuation`、`log_ref` | 重述 | 折叠 | 第 1 期保留；第 2 期由 `output_next` 取代 | 保留 |
| 输出页字段（§4.3） | 证据 | 不出现 | 出现 | 按需出现 |

这是对前序文档“所有折叠都限定在已成功且终态”的收紧：再加一个条件“输出完整”。规则写成一个谓词，三处调用，不再各写一份：

```python
def success_restatements(row, execution):
    """成功且终态时可折叠的键；输出不完整时保留续读指针。"""
    if (
        row.get("pending") or row.get("error")
        or execution.get("command_ok") is not True or execution.get("exit_code") != 0
        or execution.get("timed_out") or execution.get("cancelled")
        or row.get("state", "succeeded") != "succeeded"
    ):
        return ()
    keys = []
    if (execution.get("failure_context") or {}).get("category") in (None, "succeeded"):
        keys.append("failure_context")
    recovery = row.get("recovery")
    if isinstance(recovery, dict) and recovery.get("task_outcome_confirmed") \
            and recovery.get("task_state") == "succeeded":
        keys.append("recovery")
    if not (execution.get("output_truncated") or execution.get("output_normalized")):
        keys += ["continuation", "log_ref"]
    return tuple(keys)
```

`?verbosity=full` 不经过这个谓词，始终返回完整回执。

### 4.3 输出页字段

只在输出不完整时出现，字段保持扁平，沿用现有命名。

| 字段 | 出现条件 | 含义 |
| --- | --- | --- |
| `output_truncated` | 任一层有损失 | 现有布尔值，保留兼容 |
| `output_truncated_by` | 同上 | 按层排序的列表，取值见下 |
| `output_chars` | 含 `limit` 或 `budget` | Hub 已存尾部的字符数（现有字段，不再删除） |
| `output_range` | 含 `limit` 或 `budget` | `[start, end)`，返回文本在已存尾部中的字符区间 |
| `output_next` | 第 2 期，含 `limit` 或 `budget` | 可直接调用的续读参数 |
| `output_normalized`、`output_original_sha256`、`output_original_bytes`、`output_original_chars` | 规范化改变了文本 | 工作区改动已实现，保留 |

`output_truncated_by` 的取值：

- `upstream`：Agent 或 Hub 存储丢弃了更早的输出，这部分在任何地方都不存在。判定：Agent 上报的 `output_truncated`，或已存长度达到 131,072。
- `limit`：L3 按 `output_limit` 切尾，其余部分仍在 Hub，可续读。
- `budget`：L5 为满足响应预算收窄了窗口，其余部分仍在 Hub，可续读。

`output_original_scope` 取消：区间已经说明了指纹对应哪一段。

示例（view 2，仅列出与完整成功回执不同的字段）：

```json
{
  "output_truncated": true,
  "output_truncated_by": ["limit"],
  "output_chars": 41230,
  "output_range": [33230, 41230],
  "output_next": {"name": "query", "arguments": {"action": "job", "project": "MCP",
    "ids": ["<operation_id>"], "args": {"output_offset": 13230, "output_limit": 20000}}}
}
```

### 4.4 各层改动

**L3 读取视图。** 把现在合并成一个布尔值的两个条件分开登记：进入 L3 前 `data.output_truncated` 已为真记 `upstream`，`len(original) > limit` 记 `limit`，同时写入 `output_range`。精简视图在 `output_truncated_by` 非空时保留 `output_chars`。

**L5 响应预算：只收窄窗口，不拼接。** 用现有的 `_fit_prefix` 二分，在行边界上把最大那条输出的尾部窗口缩到刚好放得下，追加 `budget`，更新 `output_range`。正文里不再写入省略标记。

- 得到的仍是一段连续、未改写的文本，所以预算路径不需要指纹，`bound_batch_response` 里的 `record_output_original` 调用可以去掉。
- 单调性自然成立：`output_limit` 越大，返回的字符数不会变少。
- 代价是失去现在保留的头 1/5。理由：这个“头”是 L3 切尾后那一页的开头，本来就是日志中间的任意位置；第 2 期之后任意区间都可读。
- `output_budget_limited` 保留兼容。

**L4 规范化。** 维持显式开启、默认 `output=raw`。`exec` 的输出可能是 `cat` 或 `git diff` 的结果，折叠重复行会改写源码内容，这一点系统无从判断，所以不能默认开启。

**恢复指引。** `output_recovery` 的文字改为按情况给出：

- 含 `limit` / `budget`：第 2 期起由 `output_next` 取代文字；第 1 期写明已存长度和本次区间，并说明“当前连接每页约可返回 N 字符”。不再提连接地址参数。
- 含 `upstream`：写明更早的输出没有保存；需要完整日志时，重新运行并把输出重定向到运行目录下的文件，再用文件读取分页（文件读取有 SHA 和 `next_offset`，是精确的）。
- `?verbosity=full&output=raw` 保留在 `UNIFIED_TOOLS.md` 里，作为给人看的连接配置说明。

### 4.5 第 2 期：区间读取

`query job`（`process` 的 `get` / `wait`）新增 `args.output_offset`：

- 只对终态操作有效，返回 `stored[offset : offset + output_limit]` 及对应的 `output_range`。
- 运行中的操作尾部会滑动，传入时返回 `INVALID_ARGUMENTS`，继续使用 `after_output_seq`。
- 不新增权限：读取前的操作、项目、凭据和工具检查不变。
- 131,072 字符的已存尾部在默认连接上约 7 页读完，在 text=compact 连接上约 3 页。

有了它，`output_next` 才是一次真实可执行的调用，F4 才算解决。

### 4.6 输出策略按工具声明

在 `shared/core_contracts.py` 的工具类别映射旁边声明输出策略，`mcp_presentation.py` 与 `unified_contracts.py` 都从这里导入：

| 策略 | 工具 | 展示层可以做什么 |
| --- | --- | --- |
| `log` | `shell_exec`、`ssh_exec`、`tasks_run`、`vps_exec` | 相同副本改指针、显式规范化、预算收窄窗口 |
| `exact` | 其余全部内置工具（默认值） | 只做有界分页，带 SHA 与续读偏移 |
| `passthrough` | 网关命名空间下的外部工具 | 不改写，仅追加网关调用标识 |

`output_records` 增加一个由调用方传入的工具名参数：`token_view(name, …)` 本来就拿着派发的工具名，无 `tool` 字段的直返回执不必再靠 `exit_code` 是否存在来猜。新增执行类工具时只改一处，漏改的后果是“不压缩”，而不是“误压缩”。

## 5. 实施步骤

| 期 | 内容 | 文件 | 性质 |
| --- | --- | --- | --- |
| 1 | `success_restatements` 谓词，三处改为调用它 | `shared/mcp_presentation.py`、`shared/unified_contracts.py` | 纯展示 |
| 1 | L3 分开登记 `upstream` / `limit`，写入 `output_range`；精简视图保留 `output_chars` | `hub/runtime.py`、`shared/unified_contracts.py` | 新增可选字段 |
| 1 | L5 改为收窄窗口，追加而不覆盖；去掉预算路径的指纹与正文标记 | `hub/core_tools.py` | 纯展示 |
| 1 | `output_recovery` 文字按情况给出 | `hub/core_tools.py`、`shared/mcp_presentation.py` | 纯展示 |
| 1 | 输出策略表移入 `core_contracts`，去掉函数内导入 | `shared/core_contracts.py` 等 | 重构 |
| 1 | 输出 schema 如逐字段声明，补上新字段 | `shared/core_output_schemas.py` | 契约 |
| 2 | `output_offset` 与 `output_next` | `shared/core_contracts.py`、`hub/runtime.py`、`hub/operation_output.py`、`hub/unified_help.py` | 新增参数，向后兼容 |
| 3（可选） | Agent 在读流时累计总字节数与 SHA-256，上报 `output_total_bytes` / `output_total_sha256`；可选把完整日志落到运行目录 | `agent/runner.py` | 随 Agent 版本发布，旧 Agent 不上报 |

`docs/UNIFIED_TOOLS.md` 的 “Output fidelity” 一节随第 1 期同步改写。第 2 期合入前，文档不得声称任意位置的输出都可取回。

## 6. 测试与不变量

在 `tests/test_mcp_output_fidelity.py` 现有 16 个用例基础上：

1. **体积护栏。** 完整成功的 `echo ok` 回执在四种展示方式下的字节数不超过 HEAD 的数值（931 / 919 / 390 / 378）。以后任何让它变大的改动都会在这里失败。
2. **折叠条件。** 同一份回执分别置 `output_truncated`、`output_normalized`、失败、超时、取消、未完成，断言 §4.2 表格的每一格。
3. **只追加。** 构造上游已截断的页，再经过 L3 和 L5，`output_truncated_by` 依次为 `["upstream"]`、`["upstream","limit"]`、`["upstream","limit","budget"]`。
4. **单调。** 对同一份已存尾部，`output_limit` 从 1000 到 51,200 递增，返回字符数不减。
5. **区间精确。** 返回文本等于 `stored[range[0]:range[1]]`；预算收窄后首尾都落在行边界上。
6. **区间读取往返（第 2 期）。** 按 `output_next` 翻页直到 `start == 0`，拼接结果等于已存尾部；对运行中的操作传 `output_offset` 被拒绝。
7. **现有用例保持通过**：非执行工具不改写、未知工具嵌套不遍历、外部 MCP 透传、扁平化同源。其中断言“成功后仍保留 `continuation` / `log_ref` / `failure_context`”的用例（`test_mcp_token_presentation.py`、`test_unified_tools.py` 各一处，本文件两处）要按 §4.2 改写，断言预算路径带指纹的一处要按 §4.4 改写。

## 7. 不做的事

- **不删 `operation_id`、`output_seq`、SHA、幂等键。** 与前序文档一致。
- **不在失败、超时、取消、未完成状态下折叠任何字段。**
- **不默认开启规范化，不做摘要式截断。**
- **不为日志预算保留“头加尾”拼接。** 拼接出来的文本不可寻址，也无法核验。
- **不在第 1、2 期承诺完整 stdout。** 那需要 Agent 配合（第 3 期）。
- **不改变 `?verbosity=full` 的含义。** 它仍然返回未折叠的完整回执。

## 8. 设计评审时的未验证项（历史）

- 全量回归没有运行（收集到 12,602 项、633 个测试文件）。按 `CONTRIBUTING.md`，应在 Actions 手动运行 CodePier CI。
- 总结里的“11 个模块 298 项通过”未能复核，不知道具体是哪 11 个模块；Ruff 与 mypy 本次也没有重跑。
- 设计评审时尚未实现；本轮实现及验证见 §10。
- 真实 Agent 回执中 `git_status` / `git_diff` / `git_log` 的 `tool` 字段取值没有核对；若它们以 `shell_exec` 入库，则已在 `log` 策略内，否则需要在策略表里显式归类。
- 每页可返回的字符数：默认连接约 2 万来自合成输出实测；text=compact 连接约 4.5 万是按 48 KiB 单份正文估算的，没有实测。真实日志含多字节字符或大量转义时会更少。
- 各宿主取用 `content` 还是 `structuredContent` 仍未验证，沿用前序文档的结论。
- 第 3 期对 Agent 读流性能的影响没有测量。

## 9. 测量与验证证据

所有命令经 CodePier `exec` 在项目 `MCP` 内运行，操作编号可按原编号查询。

| 内容 | 结果 | 操作编号 |
| --- | --- | --- |
| 四个被改动的测试模块 | 180 passed，24.70s | `971d99eb92ab409fa1189266e910e9f3` |
| 成功回执体积，工作区对 HEAD（§3.2 F1、F3） | 见 F1 表 | `33df0a5165b8487c847fe18f2f8c16bf` |
| 预算折半不单调（F5） | 见 F5 表 | `165d9ad3158c4c2dbfafaecf3fb94863` |
| L3 静默切尾的实例（两次 `git diff`） | 开头被切，回包仅 `output_truncated: true` | `1485e7c7f6e8440b845b3a35d257b6bc`、`02f6ab511327426bb4668d8b7d00afdb` |
| 64 个引用相关模块的测试文件，第一次 | 1122 passed，198 failed，254 errors，402.75s | `76569dfddd234bdaa4f49f3800afd59d` |
| 同上，第二次（带失败原因） | 数字相同，385.56s | `92d6a0de064a4032bc7ebb41cf371bf9` |
| 失败归因与 HEAD 对照 | 见下 | `043202997ebb40c0b49492f7ca9ab62f`、`483b05f90adb40d696075b9c952c2217`、`65322a9afb4141c99cce79655619ae9a`、`e15606a4213641ba99e7c7ee21ace763`、`6eb5e957546446eebc98c7770146a176` |

64 个文件里 452 项失败或报错的归因：

- **287 项是运行环境造成的。** 经 CodePier `exec` 运行时 `TMPDIR` 位于 Agent 的项目运行时目录下，测试在其中创建临时项目被 `PROTECTED_ROOT` 拒绝；另有 11 项 `TimeoutError` 集中在 `test_adaptive_agent_integration.py`，该文件在 HEAD 副本上单独运行同样失败。
- **143 项是浏览器测试**，Playwright 的浏览器可执行文件在该运行环境里不存在。其中 `test_chat_complete_regressions.py` 的 3 项没有逐条核对报错信息，按同类归入。
- **其余 22 项**（`test_ssh_resilience.py` 13、`test_ssh.py` 2、`test_project_runtime.py` 2、`test_native_file_sources.py` 2 及另外 3 项）在 HEAD 副本上以完全相同的集合失败。

结论：这一轮没有发现由工作区改动引入的失败。但同一环境下有 452 项跑不起来，这轮结果不能替代 CI 上的全量回归。

最后一行里有两个操作以非零退出码结束：`483b05f9…` 是脚本末尾用了 `/bin/sh` 不支持的进程替换（退出码 258），`6eb5e957…` 是末尾计数为 0 的 `grep` 返回 1；它们之前的测试步骤都已实际执行，输出如上所述。评审过程中写到 `/tmp` 的临时文件与 HEAD 副本均已删除。

## 10. 实施与验收（2026-11-11）

第 1、2 期和第 3 期的流式计数 / 指纹已落地。实现保留了评审前工作区中的外部 MCP 透传、未知工具不遍历和扁平化同源保护。未改动授权、幂等键、数据库结构或默认 raw 输出策略。

| 项目 | 实现 |
| --- | --- |
| 成功折叠 | `shared/output_fidelity.py::success_restatements` 统一 `_slim`、`_present_exec` 和 compact text；失败、超时、取消、运行中均保留诊断 |
| 分层证据 | L3 区分 upstream / limit，预算只追加 budget；保留已存长度、Unicode 字符区间和来源 |
| 预算 | 二分选择连续尾部；正文不插入省略标记；同步输出指针所指向的区间元数据 |
| 工具策略 | `shared/core_contracts.py::OUTPUT_POLICIES` 声明日志工具，未知及其他内置工具默认 exact，网关继续透传 |
| 区间读取 | `query job` 的 get / wait 支持 `args.output_offset`，终态检查与已有 IAM 检查在读取前执行 |
| 可执行续读 | `output_next` 保留项目 / workspace 和原操作 ID；向前补齐较早页面，直到 start 为 0 |
| 规范化恢复 | 显式区间读取在当前连接返回 raw；无须模型修改连接 URL |
| Agent 指纹 | 在读取每个 stdout/stderr 合流字节块时累计字节数和 SHA-256；位于解码、凭据脱敏和尾部裁剪之前；旧 Agent 可不提供 |
| 帮助与契约 | 核心与旧操作参数、输出 schema、统一帮助和 `UNIFIED_TOOLS.md` 同步更新 |

### 10.1 实现中补充的边界

1. 逐字符测试发现，仅改用二分仍会在预算临界点回退：第一次附加 budget 元数据本身也占空间。现在先测量这些元数据的预算，再决定是否收窄；默认尾读在可用时对齐完整行，保证增大 `output_limit` 不会减少返回正文。显式 `output_offset` 仍按精确切片读取，只有预算层可以收窄。超长单行保留按字符窗口读取的能力。
2. 规范化与预算同时发生时，原文指纹随最终窗口重新计算；因此 `output_range` 和 SHA 始终对应同一段原文。较大的原文窗口可能需要多次 raw 续读才能重组核验。
3. `output_total_*` 指纹针对 Agent 实际接收的原始管道字节，不是脱敏后的字符串。超时、取消同样记录终止前接收的字节，不宣称未收到的字节也已保存。
4. 完整日志文件依原设计保持可选：可以显式重定向到 `$CODEPIER_RUN_DIR` 后使用文件分页；没有新增自动保存完整 stdout 的行为，也不自动重跑命令。已经丢弃的 upstream 内容仍不可恢复。
5. 已核对 Agent 的 `git_status` / `git_diff` / `git_log` 使用独立工具身份，不伪装为 shell_exec；保留 exact 策略。

### 10.2 验证证据

- 14 个相关模块：**521 passed，126.65s**；覆盖新增保真、输出存储、Agent 指纹、凭据脱敏、统一目录、外部网关、文件批量预算、操作续读/完成事件及协议兼容。操作 `0166fb7fafd94c8bb485b40b358e51ea`。
- 最后一次嵌套预算引用修正后，重跑 5 个直接相关模块：**156 passed，31.24s**。操作 `736488bc9dc840049f7e6d0e0852bb7c`。
- Ruff 检查通过；mypy 按项目配置检查的 **9 个源文件通过**。mypy 操作 `a2cd6f4109a34097abe219af9a31b4c8`。
- 小型完整成功合成回执的四种展示体积为 **787 / 772 / 282 / 267 字节**（view 2 json / compact、view 3 json / compact），低于原设计护栏 **931 / 919 / 390 / 378**。这组固定回执不包含新 Agent 的可选 `output_total_*` 证据，不能用于推断所有实际命令的总大小。
- 在本机按 64 KiB 字节块累计 SHA-256，64 MiB 用时 **27.14ms**；这是哈希计算微测量，不是端到端执行吞吐保证。测量操作 `c8f66ace1fb64282814d0cee900f1c7a`。
- 本次实现验证未运行完整仓库回归、浏览器宿主端验收或 VPS 部署。发布作业结果以 GitHub Releases 和 Actions 为准。
