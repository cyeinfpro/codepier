# dot 协作重构验收记录 · 2026-10-10

## 当前结论

代码侧重构和隔离环境验收已完成。面板添加 dot、CPD 加入、唯一任务订阅、定向消息、真实 Hub/Agent 执行、进度与结果回原话题均有测试证据。

**真实原生 dot 宿主的自主唤醒/执行尚未验收；本次没有推送、合并到用户的 main 工作区或部署生产服务。不能据本报告声称用户正在使用的真实 dot 已接通。**

实现分支：`feat/dot-collaboration-one-step-20261010`。

核心实现提交：`35d2929`。固定测试快照：`5af7a27`，已合并主线 `b2b0877240f2944bb8362d6ae4586ff1f628607c` 的手机高度及准入回归修复，不覆盖其他发布工作区。本报告是测试完成后添加的文档，不修改被测应用代码。

CodePier 工作区：`80f4b30d670a4af9856491bc8b96f519`。

## 已实现的验收点

| 用户操作或异常 | 实现与核验 |
| --- | --- |
| 面板添加 dot | 一次填写名称和确认既有项目范围，生成 CPD 加入码；此时不创建任务或新凭据 |
| 在目标 dot 加入 | 当前已授权连接绑定原面板批准，后台自动配置任务规则；返回一条原生任务订阅请求 |
| 接通后的交办 | 输入 @ 并选择 dot，直接发送；不让用户手填策略、委托、游标、租约或操作编号 |
| 连续交办与追问 | 接收对象保持选中；回复原消息可交办后续任务；取消交办后普通消息不执行 |
| 接收与结果回传 | 领取确认、主动进度、最终结果都回复原消息，并显示该 dot 名称 |
| 恢复与重复通知 | 首次订阅后立即读取待办；固定服务端接入基线；分页、Hub 重启、重复加入/读取/领取/步骤/结果均有覆盖 |
| 实际执行 | 写文件、读文件、运行命令；核对实际终态、退出码、文件内容及原操作 ID，而非仅提交模拟成功状态 |
| 故障与撤销 | 缺权限、过期加入码、快照或目标变化、跨连接接入、错误租约、撤销和未完成操作的提前报成功均拒绝 |
| 旧功能 | CPJ 仍只接入旧通知；原 Work、显式委托、发言及监控用途不提升权限 |
| 手机与桌面 | Chromium 桌面、Chromium 手机、WebKit 手机均走完整新流程；保留主线手机视口修复 |

## 最终固定快照的测试结果

依赖环境位于 `.work/dot-refactor/venv`，由 `uv pip sync --require-hashes requirements-dev.txt` 建立；没有修改其他工作区的共享虚拟环境。测试期间不再修改被测前端或重新生成静态资源。

| 检查 | 真实结果 | 日志与证据 |
| --- | --- | --- |
| 全仓非 integration、非 browser 回归 | **3943 passed，2170 deselected，11 warnings；exit 0** | `.work/dot-refactor/merged-full-unit.log` / `.xml`；操作 `f74750f12d7646f2af4e762edc4a9824` |
| 选定集成与浏览器回归 | **72 passed；exit 0** | `.work/dot-refactor/merged-integration.log` / `.xml`；操作 `1b39024935614023ab69347531587bfe` |
| 全仓 Ruff | **All checks passed；exit 0** | 操作 `179524219f2444da9190265f241e3294` |
| JavaScript 语法 | app、workflows、collaboration、collaboration-dots、collaboration-delegation 的 Node 检查成功；浏览器亦实际加载执行合并快照 | 对应 Node 检查操作及上述浏览器结果 |
| 静态资源和工作树 | 按合并后的源码生成 index/manifest；固定快照运行真实 Hub，未关闭静态资源完整性校验；提交后应用代码工作树干净 | 操作 `77c569e9e4a34d87af1170dae60ae4a5`、`179524219f2444da9190265f241e3294` |

11 条警告是 pytest/Starlette 测试接口弃用及 JUnit record_property 兼容性提示，不是测试失败。2170 个未选中项不是「已经通过的测试」，本报告不宣称运行了全部浏览器/集成用例。

主要命令：

```sh
.work/dot-refactor/venv/bin/python -m pytest -q \
  -m 'not integration and not browser' \
  --junitxml=.work/dot-refactor/merged-full-unit.xml

.work/dot-refactor/venv/bin/python -m pytest -q \
  tests/test_collaboration_dots_flow.py \
  tests/test_collaboration_join_browser.py \
  tests/test_collaboration_delegation_targets_browser.py \
  tests/test_collaboration_chatroom_browser.py \
  tests/test_collaboration_goals_browser.py \
  tests/test_hub_adaptive_admission_stack.py \
  tests/test_agentdock_integration.py \
  --junitxml=.work/dot-refactor/merged-integration.xml

.work/dot-refactor/venv/bin/ruff check .
```

开发阶段另有新流程/消费者 40 项通过、整合回归 127 项通过。它们与最终验证重叠，不能相加当作独立测试总数。开发中出现过契约字段断言错误、旧高级入口导航断言及前端改动触发静态资源清单不匹配；已经修正并在固定快照重新验收。历史失败日志保留，最终结论只依据上表终态。

## 真实执行证据的边界

`test_one_code_real_agent_two_tasks_restart_and_original_thread_results` 启动真实隔离 Hub/Agent，创建两条任务，在两条任务之间重启 Hub，再从原持久接入配置恢复。每条任务实际执行 write/read/exec 三步，共六个不同原操作；同一步重试返回同一个 operation_id。测试核对磁盘内容、命令退出码、原话题领取/进度/唯一结果。

浏览器测试还通过实际面板创建 dot、复制加入指令、输入 @ 选择、发送任务、查看进展/结果、原话题追问以及取消交办。截图位于 `.work/dot-refactor/screenshots/`，包括 `dot-join-{engine}-{width}.png` 和 `dot-results-{engine}-{width}.png`。最新 WebKit 手机结果为 390×920，已保留主线视口修复。

外部原生 dot 在这些测试中由明确标注的接收器和消费测试代码代替。这证明 CodePier 侧闭环，并不证明真实模型已接收事件并自主调用插件。HTTP 2xx、正确签名、单纯注册成功、测试事件或模式配置均不作为真实模型消费证据。

## 仍需完成的现网闭环

首先按项目发布流程将此分支的应用代码和配套静态资源部署到实际使用的 Hub，保留原项目授权和 MCP Events 设置；本次未执行推送或部署。

然后在实际面板添加一个任务 dot，将其完整 CPD 加入指令发给目标 dot。由该宿主按原生流程确认订阅并保存消费说明。用一个只读任务和一个连续任务核对真实 dot 的领取、原操作和原话题结果，再核验恢复。用户只需一次加入/宿主确认，不应手动配置回调、密钥或委托编号。

在这一步实际完成前，验收状态保持「CodePier 代码侧完成，真实原生宿主与现网待验收」。详细使用、权限边界和现有有效期/预算见 [DOT_COLLABORATION.md](DOT_COLLABORATION.md)。
