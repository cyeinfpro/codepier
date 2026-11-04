# MCP Token 优化实施记录（2026-11-04）

本次针对 MCP_TOKEN_OPTIMIZATION_2026-11-04.md 的建议实施展示和目录优化。原建议报告仍在原工作区未提交，本记录不移动、覆盖或归档它。所有变化均保持授权、SHA、幂等、原操作恢复与持久回执语义；目录隐藏不构成授权拒绝或授权提升。

## 连接参数

参数加在 `/mcp` 连接地址，组合示例：`/mcp?schemas=lean&profile=coding`。这些是展示参数，不加入执行命令参数或幂等载荷。

| 参数 | 默认 | 行为 |
| --- | --- | --- |
| `schemas=full\|lean\|none` | `full` | full 保留输出 schema；lean 使用公共输出字段，协作输入采用浅层目录；none 不发布本地输出 schema。远端工具契约不改。 |
| `profile=core\|full\|coding` | `core` | coding 发布九项编码工具；core/full 保留原完整原生目录。实际调用仍通过原始完整路由及权限检查。 |
| `view=1\|2` | `2` | v2 启用本次展示优化；v1 保留旧展示兼容路径。 |
| `verbosity=lean\|full` | `lean` | full 请求完整诊断展示，不改变持久结果。 |
| `text=json\|compact` | `json` | compact 允许受支持的 read/exec 文本原文渲染；structuredContent 保留结构化数据。 |
| `output=raw\|normalized` | `raw` | normalized 显式开启 stdout 展示规范化，标记变换及原文恢复提示；full 仍保留原文。 |

`CODEPIER_MCP_RESPONSE_BYTES` 控制已知 read/batch/stdout 的完整响应预算，默认 49,152 字节，可设 8,192–1,048,576；无效配置会明确报错，不静默忽略。批量读仍最多八项。应按返回的续读字段继续读取，不能将截断解释为文件结束。

## 已实施

- 修正输出 schema 对空 error 和 v2 展示字段的兼容，避免成功执行被宿主误拒。
- 成功回执展示去重、去除无信息诊断块；保留失败及未完成结果的诊断语义。
- 单文件和批量 read 的完整响应预算约束，保留按行续读能力。
- 新建 write 不回显完整 diff，覆盖写/edit 对大 diff 截断并明确标记。
- 输出目录 full/lean/none 和实际 coding 档位；目录指纹随展示内容变化。
- 协作 lean 输入目录保留所有已支持的顶层属性、类型和基础约束，省略巨型嵌套结构；不增加通用 options 或任何新执行权限。执行端仍使用原 TypeAdapter 和业务模型验证。
- `project_query(operation="help", tool="collaboration_query|collaboration|collaboration_work", action="具体动作")` 按需返回该动作完整精确 schema；省略 action 可列举动作。包括 execute 的嵌套工具判别契约，均不放宽服务端检查。
- 常驻 instructions 压缩至 1,602 字符；完整规则通过 `rd://workflow` 获取，view1 保留完整 instructions。保留拒绝、独立授权、容器隔离证明、租约、重放与不可信内容约束。
- JSON 文本采用紧凑分隔符。v2 常规调用不再重复服务版本；initialize/discover/tools/list 保留版本，现代协议必需的 serverInfo 以及请求关联 requestId 保留。
- 文档索引过滤锁文件、日志和生成物，大文档提示分段读取；minimal open 精简重复索引样板。

## 输出展示与恢复

stdout 默认保持 raw。显式 `?output=normalized` 才去除 ANSI 转义、折叠 CR 进度条和连续重复行，返回 `output_normalized`、原始字符数和恢复提示。`?verbosity=full&output=raw` 可按原 operation_id 查询持久原文；规范化仅作用于展示副本，不能当作无损可逆压缩。

v2 lean 的 exec/task_query 超预算 stdout 按头 20% + 尾 80% 裁剪，明确标记 truncated 和恢复提示，保留原操作编号和原输出序号。read 仍按完整行续读，不能把 stdout 的字符裁剪套用到行号或事件游标。15 项输出规范化/预算定向测试已通过。

## 目录展示与不可缩减响应

v2 的 `project_query(operation="tree", project="项目", options={"format":"compact"})` 返回相对 base 的紧凑目录，特殊字符用 JSON 引号保留边界，分页和错误信息保持；view1 返回结构化目录。tree 相关九项定向测试通过。

通用 v2 原生文本预算会收缩已知 read/batch/stdout；无法安全缩减的元数据不会静默丢弃。此时返回 `RESPONSE_TOO_LARGE` 错误内容，同时保留 operation_id、state 恢复摘要和查询同一原操作的 full 提示，避免吞掉已执行 mutation 的回执或引导重放。该路径新增两项测试通过。图片沿用独立媒体预算，不删除截图来满足文本预算；远端工具返回保持不变。

## 同步搜索

`project_query(operation="search", project="项目", query="字面关键字", limit=20, options={"file_glob":"*.py", "case_sensitive":false})` 在原 fs_search read 权限链上执行有界搜索；可传 path/offset。完整参数由 `project_query(operation="help", tool="workspace", action="search")` 获取。正常短搜索一次返回路径、行号和匹配文本；运行未结束时返回同一个 pending operation_id，使用 task_query 恢复，不能重放搜索或伪造完成状态。没有重新开放已移除的 fs_search MCP 工具名。

## 体积测量

按 `json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()` 的 UTF-8 字节测量，数字为本次代码状态，不是实际 tokenizer 计费值。

| 原生目录 | 字节 |
| --- | ---: |
| core/full schema | 90,100 |
| core/lean schema | 28,617 |
| coding/full schema | 67,035 |
| coding/lean schema | 19,554 |

当前三项协作目录已扩展到 115 个动作，测量完整约 60,018 字节，浅层输入约 29,782 字节（该项测量使用 ASCII 转义，主要为英文内容）。保留全部顶层字段和安全说明，未强行追求报告中的 6–8 KiB 目标。安全语义及兼容优先于单纯缩小阈值。

合成夹具的完整 MCP result（紧凑 UTF-8）测量：400 行相同 stdout 的 exec 从 22,754 降至 11,364 字节；新建 1,000 行文件的 write 从 25,436 降至 466 字节。原始 stdout 内容不作归一化处理。

## 验证

仅运行变更相关测试，没有重复启动全量回归或 CI。

- `test_mcp_catalog_modes.py` 原八项与 `test_mcp_resource_validation.py`：24 项通过。
- 协作 schema 自校验覆盖所有 115 个动作；新增精简目录测试同时验证字段保留、schema 合法性、未知字段及无 read scope 拒绝。
- 协作新增测试、`test_public_collaboration_contracts.py`、`test_host_parameter_schema.py` 共 160 项首次 157 通过。两项旧 coding=13 的预期更新为 coding=9，六个 profile/authorization 组合定向复测通过；help 接线完成后单项 HTTP 定向复测通过。
- 展示新增 11 项通过；旧 schema/presentation 已通过 107 项，另两项 view 适配及两项临时目录夹具修复均定向复测通过。batch 回归 32 项和实际 polling 1 项共 33 项通过。
- 同步搜索四项路由/ACL/pending/schema 定向测试通过；既有真实 Hub/Agent 搜索集成一项通过（默认临时路径被安全规则拒绝后，改用合成 `/tmp` 测试夹具，未更改安全门）。协作 help 的发布 schema 断言与非法 output 参数两项通过。
- Ruff 正确性检查和 `git diff --check` 通过。
- 原有环境缺 pytest 或 jwt 的尝试未产生测试结果；没有为此修改依赖环境。
- 未将上述单元/HTTP 测试宣称为 Claude Code 或 ChatGPT 实机验收，亦未宣称生产部署成功。

## 未实施与限制

- 预算缩减仅处理已知 read/batch/stdout 结构，不任意截断元数据、图片或协作正文；其他原生文本超预算返回保留原操作恢复摘要的明确错误。媒体保留独立上限。输出裁剪不制造字节游标，不推进 after_output_seq，也不声称变换可逆。
- 没有广泛移动带日期文档、删除旧文件或归档默认工作区未提交报告。
- 外部宿主的 lean schema/compact text 实机体验、token dashboard 前后对照仍需实际客户端验证。

## CI 目录门禁补充修复

基线 CI 41768604390 的三个目录测试使用默认 JSON 序列化（含空格和 ASCII 转义），其 95,000 字节门禁与上表紧凑 UTF-8 口径不同。修复将纯类型 anyOf 等价折叠为 type 数组，对短重复节点复用引用，并在最终展示投影后再次因子化；保留原有 $defs 名称及类型约束，不提高门禁。当前默认目录为 94,748 字节、全权限投影 94,743 字节、紧凑 UTF-8 为 86,644 字节。

只验证 test_core_tools.py、test_dot_tool_discovery.py、test_project_view_projection.py 三个模块：58 项通过；剩余一项 coding 旧 13 工具预期改为真实 9 工具后单项复测通过。包含已有 $defs、二次因子化、合法/非法类型等价和能力布尔值投影回归。Ruff 正确性及 diff 检查通过；未启动全量测试或 CI。
