# Dot 执行环境增量实施与续作记录

最新完成状态见第 8.3 节：最终提交 `da81e12` 的 19 个 CI 任务全部通过；Linux/macOS 各 8,865 项、Windows 331 项及真实容器 17 项资格检查通过。真实 Dot 新旧会话验收按用户要求不执行。第 1–7 节保留历次历史结果，不代表最终交付状态；其中 Docker 不可用仅描述当时的 Mac 默认连接。

日期：2026-10-19（UTC）
项目：CodePier MCP，源码版本线 1.30.0
分支：`release/v1.30.0-runtime-hardening`
最初续作 HEAD：`aea0c45653ed768f6ef1ea8f2e1b28ac501edb3d`
最终验收 HEAD：`da81e12d62673d6e025d546f3a9641f2f5db7109`

## 1. 第一轮交付状态（历史）

[Dot 执行环境设计](DOT_ENVIRONMENT_ISOLATION_DESIGN_2026-10-18.md) 的 W1–W4 源码增量和对应模块回归已完成；W5 的容器核验、资格检查及异常恢复已有实现与回归；W6 已有实际启动 Hub/Agent 并通过回环 HTTP 完成的开发夹具。

本轮在既有未提交工作区上继续修复，没有将原有实现重新归为本轮新增。最终定向验证覆盖 16 个测试模块，**513 passed，0 failed，0 skipped**。该结果不是最终源码的全量回归，也不构成真实 Docker、Windows 节点或 Dot 客户端验收。

本轮保留原工作区改动，HEAD 和分支未变，没有创建提交、推送、部署或切换运行配置。

## 2. 本轮完成的实际修复

| 问题 | 变更 | 结果与回归 |
| --- | --- | --- |
| 历史只读 HTTP 用例仍预期 read.lsp 帮助成功 | [test_readonly_recovery.py](../tests/test_readonly_recovery.py) 对齐当前权限合同 | 只读目录和聚合帮助不公布 lsp；点名帮助和直接调用均返回 INSUFFICIENT_SCOPE，required_scope=execute；成功帮助使用 read.file |
| 输出按字符保留，却按原始字节标记截断 | [runner.py](../agent/runner.py) 在追加解码、脱敏文本时记录实际舍弃 | 70000 个汉字完整保留时 output_truncated=false；131073 个字符只保留末尾 131072 个并标记 true；保留原字符限额和脱敏顺序 |
| Docker 不明确状态被当成已确认 | [container_runtime.py](../agent/container_runtime.py) 对要求字段做精确类型比较，并要求 Running is False | None、0、空字符串不能证明已停止；未知工作负载保持 unconfirmed，不能冒充成功 |
| 畸形 Docker 发现与检查数据出现普通 Python 异常 | 同一驱动补充 RestartPolicy、CapDrop、SecurityOpt、info、镜像 Config 和 Id 的结构检查 | 已覆盖异常响应按 RUNTIME_ISOLATION_UNVERIFIED、RUNTIME_ENGINE_UNSUPPORTED 或 RUNTIME_IMAGE_UNSUPPORTED 返回，并保持启动前拒绝 |
| seccomp 使用子串匹配 | 按逗号分隔后的完整 name=seccomp 标识判断 | 继续接受合法的 name=seccomp,profile=builtin，拒绝 name=seccomp-unconfined |

新增测试位于 [test_dot_runtime_diagnostics.py](../tests/test_dot_runtime_diagnostics.py)（2 项）和 [test_container_runtime.py](../tests/test_container_runtime.py)（20 项）。容器改动针对本次已确认的类型和状态缺陷，不声称对所有可能的 Docker JSON 做完整 schema 验证。

文档同步：

- [PROJECT_RUNTIMES.md](PROJECT_RUNTIMES.md)：补清洁环境空值与保留变量、当前权限发现、可信诊断路径及容器资格说明。
- [RELEASING.md](RELEASING.md)：去掉过时的“十项”描述，以共享 mandatory checks 为准。
- [设计 0.4](DOT_ENVIRONMENT_ISOLATION_DESIGN_2026-10-18.md)：更新实际工作包状态及剩余实机验收条件。

## 3. 已有增量的核对结果

续作开始时，运行时、诊断、发现、索引和资格相关修改已经存在。本轮保留并复核了这些内容：

- clean 环境的合法空值、保留变量、Windows 大小写、venv 启动路径、私有目录和只读 readiness；显式 native_compat 保持既有语义。
- diagnostics 只从可信项目根、明确 cwd、已知直接输出工具或当前容器映射推导位置；未知 shell 内部 cd 或子进程改变 cwd 时，不猜测相对路径。
- list、help、schema 和能力提示使用统一的当前授权策略；实际调用仍执行真实权限校验。
- 默认根索引先过滤明确元文件，再计算根条目预算；指令、skills、文档和有意义目录保持可发现。
- 容器原操作恢复、重复取消后的控制任务收尾、真实 workload/transport 结果区分，以及资格脚本的归档与实际源码一致性检查。
- `shared/runtime_qualification.py::CHECKS` 当前共 17 项；资格脚本、源码归档记录和发布晋级消费者复用同一个 `verified_receipt()` 校验入口。

## 4. 回归结果与适用范围

### 4.1 本轮红绿验证

新增 22 项用例先在未应用两处源修复的代码上执行：**20 failed，2 passed**。失败实际复现了中文输出截断误报、不明确容器状态被接受和畸形响应抛普通异常。随后应用源修复，再执行完整相关模块。

| 阶段 | 范围 | 结果 | 原操作编号 |
| --- | --- | --- | --- |
| 新增用例复现 | 输出 2 项、容器 20 项 | 20 failed，2 passed；符合本次缺陷复现目的 | `3369d64ce2eb4185970f77c9e96cf72e` |
| 最终运行时组 | 10 个模块，含全部 22 项新增用例 | 376 passed，31.75 秒 pytest 时间 | `ab95e93508874136ae9e20a0b31ef176` |
| 最终集成组 | 6 个模块，含只读 LSP 和回环 HTTP 开发流程 | 137 passed，36.45 秒 pytest 时间 | `4203f09e16484ee1bb44f4d0b6cde76d` |

两组最终回归为不重叠模块，共 513 项。managed runner 均确认 exit_code=0、timed_out=false、forced_termination=false、cancelled=false、cleanup_pending=false，原日志未截断。此前单独执行的 readonly 文件 15 passed 已包含在最终集成组内，不重复计入 513。

运行时组：

```text
tests/test_project_runtime.py
tests/test_runtime_result_contracts.py
tests/test_shell.py
tests/test_dot_runtime_diagnostics.py
tests/test_dot_environment_contracts.py
tests/test_dot_environment_windows_core.py
tests/test_container_runtime.py
tests/test_dot_container_qualification.py
tests/test_ci_source_artifact.py
tests/test_ci_promotion.py
```

集成组：

```text
tests/test_dot_tool_discovery.py
tests/test_dot_tool_discovery_http.py
tests/test_dot_development_workflow_http.py
tests/test_mcp_tasks.py
tests/test_core_output_schemas.py
tests/test_readonly_recovery.py
```

两组均通过 `scripts/managed_test_run.py` 运行，使用 `.venv-hardening/bin/python -m pytest -q -p no:cacheprovider --tb=short --junitxml=...`，保留 JUnit、output.log 和 managed-run.json。精确 argv 和环境边界在原 manifest 中。

### 4.2 其他检查

| 检查 | 结果与边界 | 原操作编号 |
| --- | --- | --- |
| Ruff | 最后源码修改后，agent、hub、shared、scripts、tests 全部通过 | `f46b644db80845f99ae8240b99cddba0` |
| mypy | 既有配置的 3 个源文件通过；不表述为全仓库类型覆盖 | `8ee06575440d454e84774027770a5af1` |
| 前端格式与构建 | format:check、MCP Apps build、integration assets、web assets、web assets --check 五阶段通过 | `d76442ee053c44318f823e72f331456a` |
| 发布源码检查 | 续作早期 check_release.py 已通过；文档收尾后的最终源码清单和检查结果另存 verification/summary.json | `a8dc85c8fddf4797a09c1e23d594832e`（早期） |

前端构建没有引入生成资产差异。最终静态、链接、源码清单和逐文件增量记录见本节下方的受控证据路径，不通过修改断言、删除测试或放宽限制制造通过。

### 4.3 历史全量结果保持原样

历史报告 `.work/dot-environment-20261018/full-v4-compact.json` 记录：

- collected=8820，passed=8818，failed=2，skipped=0，missing=0。
- 429 个模块、437 个执行 job，耗时 7661.467 秒；没有超时、强制终止、取消或待清理 job。
- 两个失败分别为只读 LSP 旧预期和真实资源准入测试。
- `verified=false` 保持不变。

本轮已用当前回归关闭 LSP 失败。资源准入的实机条件仍不满足；本轮没有重新跑完整 8820 项，也没有把历史通过和后续定向通过拼接为一次最终全量成功。

## 5. 剩余验收条件

| 项目 | 当前可观察事实 | 完成所需证据 |
| --- | --- | --- |
| 真实资源准入 | 历史测试在等待 project_limit 至少 129 的准备阶段遇到持续宿主压力；本轮 27 次只读采样 CPU busy 为 98.86%–100%，0 个健康样本 | 在符合原稳定健康条件的宿主重新执行原资源测试，保留原阈值、取消／重启和精确清空断言 |
| Docker | 当前 clean 环境能找到 Docker CLI；默认 server 探测 exit=1、不可达、未超时 | 获准的显式 profile、可用受支持 daemon、固定本地镜像及最终源码归档；实际执行全部 17 项检查并确认清理 |
| Windows | 核心环境与路径回归已在当前宿主执行，其中 Windows 行为通过受控模拟覆盖 | 在真实 Windows 节点执行适用矩阵并记录实际版本与运行回执 |
| Dot 既有与新建会话 | 回环 HTTP 集成夹具通过；未取得真实 Dot 客户端配对数据 | 按设计第 11.3 节分别记录客户端版本、工具目录、完成率、重复确认、断线恢复、重复副作用及日志往返 |
| 合入与发布 | 工作区仍未提交，最终源码仅有本轮定向验证 | 沿现有贡献／发布流程取得最终提交对应的完整回归和所需资格证据，再分别记录合入、部署和生效 |

资源观察在约 83 秒时已明确取消并核对原操作，以释放源码读锁继续开发；`actual_resource_test_started=false`。没有降低调度阈值，没有停止宿主系统服务，也没有用模拟采样替代真实资源验收。

Docker 观察仅适用于当前 clean 环境的默认连接，不能据此推断所有 Docker context 或主机均不可用。本轮没有启动 daemon、安装组件、拉取镜像或改写 runtime profile。

## 6. 源码、运行时和证据关联

本轮最初保存了 31 个既有修改／新增文件的 ZIP、SHA 清单、git status 和 diff。最初工具 baseline 因证据目录体积达到预算而不完整，因此以显式 `pre-edit-files.zip` 与 `pre-edit-manifest.json` 作为既有未提交改动的保护依据；原来干净的文件以 HEAD 为本轮差异基线。

最终测试覆盖的主要修复文件 SHA：

| 文件 | SHA-256 |
| --- | --- |
| agent/runner.py | `2a3521260367241d1854bbbe6145c64cdaca9114a5968cfacf51d0d3df1af446` |
| agent/container_runtime.py | `440abeb63ce43017aecb84a86d933b96a6c890262227cdf4f5fd4f33d55f0860` |
| tests/test_dot_runtime_diagnostics.py | `1435b1e5ed39c140f66296c7a53b281145c1790c500e1e103061dae12ccb3966` |
| tests/test_container_runtime.py | `227e6c54adb78dfd1bc2d87b4723d75abcf1b8c44d3c3fa5166a976e4a2171d8` |
| tests/test_readonly_recovery.py | `632493a331649c196699a0b8687c24d028b3120a5aad4b8e62637c7a18a5c220` |

执行回执实际为 `project_clean_v1`、`filesystem_scope=execution_account`、`isolation=none`：

- runtime_ref：`59669081b6e23a67034df98c71cc344775648a23fff36af3b4d72d749435a3cd`
- revision：`a82cf62ce80ccfd9f57e1ca528daa88415f6ddb099073cd60910af767599ed71`

源码修复不自动证明已运行的 Hub/Agent 已加载新实现；部署与加载证据另行记录。

受控证据目录：`.work/dot-environment-20261019-continuation/`。主要条目为：

- `pre-edit-files.zip`、`pre-edit-manifest.json`、`pre-edit-status.txt`、`pre-edit.patch`
- `regression-red/`、`runtime-green/`、`integration-green/`、`readonly-green/`
- `build/`、`resource-readiness/summary.json`、`docker-observation/summary.json`
- `verification/summary.json`、`verification/source-inventory.json`、`verification/turn-changes.json`、`verification/final-status.txt`

这些证据保留在现有工作区的受控位置；公开文档只记录必要结论，不包含凭据、私人 socket 路径或真实越界文件内容。

## 7. 2026-10-19 后续开发与真实容器验收

本次通过 CodePier 的 MCP 项目恢复工作，继续使用上述分支和 HEAD，保护了开始时 35 个修改／新增文件。没有切换分支、重置工作区或改写原有全量失败记录。

### 7.1 增量修复与回归

`agent/container_runtime.py` 补齐了三个具体缺口：

- `RestartPolicy.Name`、挂载类型／目标及创建后 inspect 的单条记录必须具有正确结构；畸形数据返回 `RUNTIME_ISOLATION_UNVERIFIED`，不再泄漏 `AttributeError`／`TypeError`。
- 容器环境拒绝非字符串、缺少等号、空键、NUL 和重复键，避免字典的“最后值覆盖”规则掩盖工作负载可能读取的另一个值。
- inspect 失败后仍只按已确认的完整容器 ID 清理；清理不确定继续返回 `RUNTIME_CREATION_UNCERTAIN` 并保留原回执，同一操作不能重新创建。

新增 13 项回归先得到 **9 failed、4 passed**，随后修复。最终三个不重叠测试组共 **510 passed、0 failed、0 skipped**：

| 组 | 范围 | 结果 |
| --- | --- | --- |
| container-green-v2 | container_runtime、dot_container_qualification | 86 passed |
| runtime-green | project_runtime、runtime_result_contracts、runtime_hardening、dot_runtime_diagnostics、dot_environment_contracts、dot_environment_windows_core、ci_source_artifact、ci_promotion | 287 passed |
| integration-green | dot_tool_discovery、dot_tool_discovery_http、dot_development_workflow_http、mcp_tasks、core_output_schemas、readonly_recovery | 137 passed |

Ruff 全仓指定目录检查和配置中的 3 个源文件 mypy 检查通过。第一次绿色组命令误列了不存在的 `test_runtime_control.py`，退出码 4、未执行测试；保留该失败记录，纠正文件列表后执行上表的 container-green-v2，没有删除或放宽测试。

### 7.2 真实 Linux 容器资格

在项目已授权的 Linux aarch64 节点上，使用已有 Docker 29.5.1 和本地固定镜像完成资格检查。使用独立临时项目、临时虚拟环境与显式 profile；执行账号和工作负载 UID/GID 均为非 root 的 1000。Python 3.11.2 运行验收程序，固定镜像中的 Python 为 3.12.15；没有将该环境表述为 CI 的 Python 3.13 矩阵。

测试沿用 `scripts/verify_project_runtime.py`，实际通过共享清单的全部 **17 项检查**：非 root、指定工作区、双向读写、网络限制、不可变镜像、启动前检查、原操作幂等、真实退出码、超时、清理、宿主绝对路径／相邻项目／跨界链接／管理 socket 拒绝、主动取消、原操作查询及归档与执行源码一致。

`verified=true`、`cleanup_pending=false`、`source_files_unchanged=true`。额外按本次原操作编号计算容器名并核对 daemon：没有遗留测试容器，scratch 已清空。没有重启生产服务、切换项目运行配置、安装系统组件或拉取镜像。

源码由公开打包器筛选、哈希校验后传送；节点重新构建的 ZIP 与本地逐字节一致。节点临时 Git 仓库仅用于生成与实际源码对应的快照 commit，它不是当前开发分支的提交、远程推送或发布。公开打包规则不包含这两份带日期的设计／续作记录；文档收尾后重新打包的 SHA 与已验收归档相同，因此最终资格复用该次完整成功运行，不重复启动相同工作负载。最终 archive SHA、source inventory、快照 commit、原操作和清理结果以受控证据为准。

证据目录：`.work/dot-environment-20261019-finalization/`。

- `pre-edit.zip`、`pre-edit-manifest.json`、`pre-edit.patch`：本次开始前的工作区保护。
- `red/`、`container-green-v2/`、`runtime-green/`、`integration-green/` 及同名 JUnit：真实红绿结果和进程清理回执。
- `real-container-proof.json`：文档收尾前首次真实通过。
- `real-container-final-proof.json`、`final-source-full.zip`、`final-public-check.json`、`summary.json`：最终源码归档、真实资格、源码检查及本次汇总。

传输期间的 SSH 认证失败、旧 Python 解包 API 不兼容、打包目标不符合仓库目录约束均保留原操作。认证失败发生在远端执行前；续传按文件内容核对，不重放结果未知的工作负载。最终结论来自完整成功的资格运行。

### 7.3 仍未完成的验收与交付

- Mac 真实资源准入：本次 8 次只读采样没有健康样本，有效 CPU busy 为 99.52%–99.68%；未启动高并发资源验收，未改调度阈值或停止宿主服务。
- Windows 实机矩阵与真实 Dot 新旧会话配对验收：仍无完成证据；受控模拟、回环 HTTP 和当前 Codex 插件调用不能替代它们。
- 最终完整回归、开发分支提交／合入、远程发布及生产部署：尚未完成。510 项定向回归与容器资格不能改写历史全量 `verified=false`，临时快照 commit 也不能用作当前分支的发布证明。


## 8. 2026-10-19 最终提交与跨平台 CI

本节继续保留上文各轮历史结果，不将旧失败、定向通过与新的完整运行拼接为一次成功。

本轮已在现有候选分支 `release/v1.30.0-runtime-hardening` 提交并推送公开源码改动，提交为 `e4e9573ce84dcd1e949264fa2d5edd6baaa447f6`。30 个受公开打包清单管理的文件纳入提交；5 份带日期的本地设计和验收记录按贡献规范留在工作区。提交前保存完整归档、逐文件 SHA 清单及补丁，证据位于 `.work/dot-environment-20261019-ci/`。

补充执行 5 个受工具发现变化影响的完整模块，结果 **62 passed、0 failed、0 skipped**，包括 coding integration、collaboration delegation bridge、computer integration、core tools 和 upstream integration。Ruff、现有配置 mypy、前端格式检查和构建、生成资产一致性检查均通过。

随后显式触发同一提交的完整 CodePier CI：

- Run ID：`39205497856`
- 地址：https://github.com/cyeinfpro/codepier/actions/runs/39205497856
- 触发方式：`workflow_dispatch`，候选分支；不是 main 合入或正式发布。
- 本节创建时运行仍在进行，最终结果以本节后续记录及原始回执为准。

用户在本轮明确说明“真实的就不用了”，因此真实 Dot 新旧客户端会话配对验收已移出本次完成条件。它没有被标记为已执行或通过，回环集成测试仍仅代表其自身覆盖范围。


### 8.1 完整 CI 暴露的确认卡片刷新缺口

首轮候选 CI 的 Linux 普通分片 1 得到 1,826 passed、1 failed、0 skipped、0 missing。失败用例是 `test_owner_task_context_flow.py::test_plan_reply_resumes_original_work_and_clear_next_request_needs_no_question[chromium-1440]`：后端已确认回复、继续原 delegation 并通过原操作幂等断言，但页面 15 秒后仍显示“等待你确认这份方案”。原失败回执保存于 `.work/dot-environment-20261019-ci/linux-shard-1/`，没有重试该失败分片来改变结论。

可控网络时序用例复现了两个缺口：确认保存后没有主动核对原问题，仍等待轮询退避计时器；已发出的旧同步请求返回旧投影后，也没有一次后续核对。Chromium/WebKit 的有效红色复现共 4 failed，原待确认卡片保持旧状态。首次测试夹具漏设消息排序字段导致卡片缺失，该记录保留在 `recovery-red/`，修正夹具后的 `recovery-red-v2/` 才作为本缺陷的红色证据。

`web/collaboration-relay.js` 现在在服务端确认保存原问题的回复后发起只读同步；若已有同步，则在其结束后合并为一次后续核对。同步流本身确认待发回执不触发递归刷新；离开原页面／授权世代后不发送过期核对。没有重新发送正文、新建任务或把客户端推测写成服务端确认状态。生成的面板资源及缓存指纹同步更新。

最终 4 个完整相关测试模块共 **64 passed、0 failed、0 skipped**，其中新增的 8 项覆盖 Chromium/WebKit 的退避、旧成功响应、旧失败响应及页面离开四种时序；原真实 Hub/Agent 浏览器流程也通过。Ruff、mypy、前端格式与构建、生成资产一致性、公开源码和兼容面板更新归档检查通过。

修复已提交并推送至同一候选分支，提交为 `5fbc0b5ff073d7413cbccaf71b74004bfaba6ff2`。完整源码 ZIP SHA-256 为 `47230e96bf4242d0767cb03768a379cf651443e085d665a91fa2c576b2ae3b0e`。前一提交的专项通过不自动代表此提交全量通过；新一轮完整 CI 的结果另记。


### 8.2 WebKit 模型选择点击丢失

第二轮完整 CI（`39209512192`，提交 `5fbc0b5ff073d7413cbccaf71b74004bfaba6ff2`）的 Linux 汇总为 **8,863 passed、0 failed、0 skipped、0 missing**；macOS 仅 `test_claude_browser.py::test_actual_claude_questions_approvals_models_and_resume[webkit]` 失败，实际症状为点击模型按钮后没有打开目录。原确认卡片缺陷所在 Linux 分片已通过，第二轮整体仍保留为 failure。

通过真实鼠标按下、延迟模型目录回复、鼠标松开复现相同时序：Chromium 通过，WebKit 失败。根因是 `chatSettings()` 在按下期间用 `textContent` 替换了模型名称的文本节点，WebKit 丢失正在进行的点击。修复在更新名称时保留该文本节点，继续使用浏览器原生 click 行为，不以提前执行鼠标按下动作或自动选择模型替代点击。

红色证据：`catalog-probe/`（1 failed、1 passed）。绿色定向证据：`catalog-green/`（2 passed）。最终完整相关模块证据：`catalog-modules/`（**87 passed、0 failed、0 skipped**，包含 Claude 真实 Hub/Agent 浏览器流程、模型目录、键盘及 WebKit 模块）。Ruff、mypy、前端格式／构建、生成资源一致性及两种公开源码归档检查均通过。所有证据均位于 `.work/dot-environment-20261019-ci/`。

修复已提交并推送至原候选分支，提交为 `da81e12d62673d6e025d546f3a9641f2f5db7109`；完整源码 ZIP SHA-256 为 `d114bc18c106d3cc2189f45107420d271c1fc7030f3b197edab96abc8cf23d0b`。第三轮完整 CI 为 https://github.com/cyeinfpro/codepier/actions/runs/39213224083 ，运行证据保留在 `candidate3/`。本段创建时运行尚未完成，不将两次旧提交的通过结果拼接成最终源码的完整通过。


### 8.3 最终交付与完整验收

最终提交 `da81e12d62673d6e025d546f3a9641f2f5db7109` 已推送到 `release/v1.30.0-runtime-hardening`，本地 HEAD 与远程分支逐字核对一致。第三轮 CodePier CI 的 **19 个任务全部 success**，运行 ID 为 `39213224083`，attempt=1：

https://github.com/cyeinfpro/codepier/actions/runs/39213224083

| 验收 | 最终结果 | 范围 |
| --- | --- | --- |
| Linux 完整回归 | 8,865 passed，0 failed，0 skipped，0 missing | 6 个分片，覆盖率已合并，恰好一次执行及源码一致性校验通过 |
| macOS 完整回归 | 8,865 passed，0 failed，0 skipped，0 missing | 同一测试清单与同一源码归档；包含真实资源准入和真实 10/30 分钟进程 |
| Windows | 331 passed，0 errors，0 failures，0 skipped | 仓库规定的核心子集；真实 PowerShell 和临时计划任务恢复均通过；未测试重启后登录前运行 |
| 真实容器资格 | 17/17 通过 | 最终提交与完整公开源码 ZIP 绑定；源码未变、清理完成 |
| Authentik | 真实隔离提供方验收通过 | 两个普通用户、登录、授权及移除组后的撤权 |
| 同提交证据复核 | verified=true，eligible=true | 仓库 `check_ci_promotion.py` 从 GitHub 只读复核提交、tree、工作流、原始执行 attempt、工件摘要及源码内容 |

两平台完整回归的 collection SHA 均为 `c3ad8e278a6614384d67215d62ae781d01ef206b00c45a918c8a1a853cc60d58`；完整 ZIP SHA 为 `d114bc18c106d3cc2189f45107420d271c1fc7030f3b197edab96abc8cf23d0b`。前两轮失败保留原结论，最终通过完全来自第三轮同一提交的完整运行，没有拼接旧提交的通过用例或重试失败测试制造成功。

最终证据目录为 `.work/dot-environment-20261019-ci/candidate3/`：`run.json`、`ci-proof.json`、`final-summary.json`、两平台 `complete-*/`、`public-source/`、`verified-assets/`、`windows/`、`authentik/`，以及两处原失败用例所在的远程分片回执。候选资格的只读校验不等于已合入 main 或已发布。

本次开发和约定验收已完成。真实 Dot 客户端新旧会话配对按用户明确要求不执行，不记为通过。生产发布、部署和项目配置切换未执行；5 份带日期的本地设计／验收记录按公开贡献规范保留在工作区。
