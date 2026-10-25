# MCP 项目运行时：实施与验收

日期：2026-10-16（Asia/Taipei）。状态：源码实现与本地回归完成；未发布、未部署、未修改生产 Agent 配置。真实 Docker 与真实 Dot 长会话验收未完成。

## 1. 本轮交付

以 `docs/MCP_PROJECT_RUNTIME_ABSTRACTION_RESEARCH_2026-10-16.md` 为依据，在本轮开始时已有工作目录上增量实现，没有重置仓库或覆盖其他任务的未提交改动。

工作目录 HEAD：`c6ea62dc8ef9b7d69060b35c83f9672a311dd77a`。此提交号不是新实现的提交号；本轮新改动尚未提交。

| 目标 | 已落地实现 | 说明 |
| --- | --- | --- |
| 可预测执行 | `agent/project_runtime.py`、config/shell/runner/action_readiness | native_compat 保留旧语义；project_clean_v1 使用明确 PATH、非登录 Shell、私有 HOME/tmp/cache；诊断与执行复用环境 |
| 真实受限后端 | `agent/container_runtime.py` | 固定镜像、指定项目挂载、非 root、只读根、网络与资源限制；inspect 后才启动；不回落原生 Shell |
| 原权限链路 | `hub/project_runtimes.py`、`shared/runtime_requirements.py`、room_scope/runtime | 严格模式绑定经认证配置修订，持久化原要求，发送与启动再次检查；配置变化、旧 Agent 与跨目标不能降级 |
| 结果与恢复 | `shared/runtime_results.py`、mcp_presentation/output schemas、runner/runtime | 原始输出不改写；failure_context、continuation、log_ref、运行时修订和真实工作负载退出分开记录 |
| 项目能力发现 | coding_reviews/core_contracts | open 提供现有 search/changes/validation/tasks/artifacts 帮助入口，不新增通用执行器或授权体系 |
| 中途连续执行 | `hub/collaboration/dot_chat.py` | 原连接契约带轻量 runtime_contract；原任务、checkpoint、lease/fence、operation 沿用 |
| 迁移与诊断 | `agent/runtime_config.py` | doctor、只读预览、显式 apply+SHA、保留私有备份；不改变身份、授权根、Shell 开关或其他项目配置 |
| 证据与过期 | source_versions、managed_test_run | 运行时改变使旧验证不再代表当前通过；包装器提供项目相对元数据，原日志/恢复 manifest 保留 |

没有伪造云环境、篡改日志或把命令标成只读。文件正文、SHA、拒绝、幂等、租约和平台决定的真实语义保持不变。

## 2. 最终回归

最终操作：`67678b3219624ffea3421391b5506f99`，退出码 0。

| 项目 | 实际结果 |
| --- | --- |
| pytest | **638 passed，0 failures，0 errors，1 skipped**；27 个测试模块，共 639 个用例 |
| Ruff | 本轮新增/修改 Python 文件检查通过 |
| 工具目录 | **94,217 字节**，保持原 `<95,000` 预算；13 个公开基础目录输出 schema 校验通过 |
| 副作用声明 | exec 保持非只读、destructive/open-world 声明，不用改注解消除审批 |
| 代码稳定性 | 测试前后 **740 个 Python 文件 SHA 一致**，没有测试中途修改代码 |
| 测试清理 | cleanup_pending=false；无测试超时、强制停止或取消 |

证据路径：

```text
.work/runtime-implementation-20261016/final-20261016-01/verification.json
.work/runtime-implementation-20261016/final-20261016-01/junit.xml
.work/runtime-implementation-20261016/final-20261016-01/ruff.log
.work/runtime-implementation-20261016/final-20261016-01/source-before.json
.work/runtime-implementation-20261016/final-20261016-01/source-after.json
.work/runtime-implementation-20261016/final-20261016-01/runs/run-509a3719c28742f2ba5b9318a3878e55/output.log
.work/runtime-implementation-20261016/final-20261016-01/runs/run-509a3719c28742f2ba5b9318a3878e55/managed-run.json
```

### 2.1 真实进程/传输联调已验证

临时隔离测试实例运行真正的 Hub 和 Agent，通过原生 MCP 读写和执行：文件工具写入后，清洁环境命令读取并修改同一文件，文件工具读回实际内容。测试刻意配置不存在的个人登录 Shell，验证选定 clean profile 不依赖它。

还覆盖原 operation_id 重复查询不重复写入、非零退出与真实原始日志、标准 Tasks/轮询契约、运行时修订变化导致 context 失效但历史回执不变、超时与进程回收、保留原权限拒绝、严格范围与配置修订持久化。

模拟容器 inspect 测试覆盖配置被削弱、额外挂载、IPC/socket/硬链接、扫描预算、镜像/工作负载不一致、结果不明与清理状态。这些模拟检查不构成真实 Docker OS 隔离验收。

### 2.2 唯一跳过项与未验收范围

真实容器用例 `tests/test_container_runtime.py::test_real_container_acceptance_with_explicit_approved_profile` 因未配置受批准的真实容器 profile 而跳过。本轮只读探测显示 Docker CLI 存在，但 daemon 不可用；没有拉镜像、开启 Docker Desktop 或把模拟结果当作真实隔离通过。

真实容器测试代码已提供，要求显式 `CODEPIER_REAL_CONTAINER_PROFILE`，会实际验证共享读写、非 root、网络限制、退出码、幂等与超时清理。不能把“配置可解析”当作该测试通过。

未执行真实 Dot 新旧长会话、生产工具目录刷新、Windows/Linux 完整矩阵或生产 Hub/Agent 更新。这些均不是本地 loopback 的等价项。

### 2.3 中途失败均已保留并修正

前序测试曾发现：诊断实现位置调整后测试探针未迁移、工具目录超过预算、文件读写夹具缺少原协议版本、Unix socket 测试路径过长、schema 压缩后布尔能力映射未适配引用。

均已修正后重新验证；没有删除原拒绝断言、修改副作用注解或放宽目录预算。相关历史失败日志保留在 `.work/runtime-implementation-20261016/tests/`，最终结论来自上表的独立完整复核，而不是把不同运行的通过数相加。

## 3. 有界完整变更审阅

本轮实现与使用文档的审阅包含 **29 个文件**，其中 13 个新增，增量 2,308 行、删除 60 行；本验收报告另计。

基准是本轮修改前逐文件保留的实际内容，不是 Git HEAD 差异，避免把原先未提交改动算成本轮实现。

```text
.work/runtime-implementation-20261016/baseline.json
.work/runtime-implementation-20261016/baseline/
.work/runtime-implementation-20261016/review/changes.json
.work/runtime-implementation-20261016/review/runtime-implementation.patch
```

补丁 SHA-256：`d26acf60d83847b0bdd21b9a9a80770f161709a315afbf70abc6331cf68f6375`。

CodePier 常规基线 `839f29ac6a7e444682de5b9d583e29bd` 受字节预算影响，不能当作完整仓库快照；上述逐文件审阅补足本轮实际改动的 before/after SHA。补丁仅适用于核对过对应 before SHA 的工作目录，不能盲目覆盖更新的上游或其他分支。

## 4. 当前上线状态

- 源码实现、本地定向回归和真实 loopback Hub/Agent 联调：完成。
- Git 提交、推送、版本发布、生产部署：未执行。
- 生产 Agent 配置迁移或全项目默认切换：未执行。
- 真实容器隔离与真实 Dot 会话验收：未完成，见上文具体门槛。

因此不能说当前生产 Dot 已使用新运行时，也不能保证模型永远无法推断操作系统。要取得实际体验变化，需要先核对目标运行构建，在明确的项目上使用新配置并完成客户端验收，不能用旧工作目录覆盖版本不同的运行服务。

详细配置、命令和迁移边界见 `docs/PROJECT_RUNTIMES.md`。本轮没有创建第二套授权或任务系统，没有自动重新加入房间、重新订阅、取消原任务或删除用户数据。
