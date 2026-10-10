# 性能验收：可重现基准与证据边界

本页规定下一轮性能验收。脚本已准备；纯 mock 测试通过不代表真实压力矩阵通过，更不代表 ChatGPT、公网、生产后端或部署收益。P95 降低 50%、成功吞吐提高 2 倍只是待测目标。没有同条件真实 before/after，不填收益数字。

## 安全与执行条件

- 只用现有 Python/依赖，不安装软件。所有输出必须是当前工作目录下全新的 .work 子目录。
- 不连接生产 Hub、真实账户或公网后端。脚本没有可传入外部 endpoint、真实凭据的参数；HTTP fixture 仅绑定 127.0.0.1。
- Hub/Agent 基准通过 central tests.support.running_stack 建立一次性 fixture。它会在输出目录的合成 ProjectAlpha/Beta/Gamma 中创建初始 Git 提交、临时用户/授权、配对数据与配置。不是修改用户仓库、生产账户或实际凭据。
- fixture 原始日志、数据库、配置仍可能含合成密钥。不得整目录上传或公开。分享前单独审阅 manifest、summary 和仅有测量字段的 JSONL；host-context 也不得填写秘密或私人内容。
- 当前运行节点曾为 10 核、loadavg 80–100，并与生产低延迟工作及旧完整回归争用。此状态不能称为“安静”。未获得合适窗口前，只跑下述纯 mock 单测，不启动真实 Hub/Agent 或 HTTP 压力。
- 默认预检 5 秒，采样 loadavg；任一样本的一分钟 load/CPU 数 > 1 则写 blocked_by_host_load 并以 2 退出，不启动 fixture。--allow-noisy 仅保留有明确噪声标签的诊断样本，不能解除比较质量要求。预检低负载也不能证明没有其他任务。
- 同一时刻只运行一个变体。禁用测试执行器并行、后台批量启动及自动失败重跑；错误必须保留原始证据。脚本不会修改部署或安装依赖。
- 脚本依赖 Unix 的 ps、loadavg 和 resource API；未声称 Windows 等价测量。

## 两条独立测量线

### Hub/Agent 真实 fixture

scripts/benchmark_hub_agent.py 的同一份文件、同一解释器分别传 --repository 指向前后完整 checkout。产品导入与 fixture 子进程 cwd 都属于被测 checkout；harness 不复制到旧 checkout。

已知 before：完整提交 b44543ec9e64240e572d031defaec1f6cd09e4b0 的独立只读 clean checkout。after：本候选冻结源码及未提交补丁。不得只用分支名或缩写提交充当版本证据。

每格包含：
- 并发 8、16、32、64、128；可单独指定 1 作诊断，不能替代正式矩阵
- read：每 worker 独立文件，校验读回 SHA
- read_write：每 worker 每四次有一次 SHA-checked 写入，其他是读；不会把同一文件冲突误算成调度吞吐
- long_short：每八个 worker 有一个每四次发一次 0.4 秒合成任务，其他为短读；短/长样本分开汇总
- 复用测量 HTTP client 的协议 warmup 完整等到终态后才开始计时
- 每个样本保留操作 ID、worker/step/kind、总时延、错误类型、poll 数、原 ID 恢复和状态
- Hub queue：同 Hub 时钟的 admission(created) 至首 dispatch；Agent queue：同一执行尝试的相对 accepted/wait 至 executing。attempts 不等于 1 时不混算 Agent 时间，只保留不可用原因
- 真实断 HTTP waiter：在 fixture 数据库看见原 idem 对应 running 操作后取消等待，仅 task_query 原 operation ID；核对该 idem 只有一条 operation、2 秒合成任务只追加一次计数

初次 HTTP 异常、poll 拒绝、未知/缺失状态及错误 receipt 均停止所有 worker 的新发起。只允许查询已知原 ID；初次响应丢失时，仅从该 fixture DB 的精确 idem 查回原 ID，不重新调用原工具。恢复成功也保留初次失败样本，不变绿。当前 phase 清理后不继续下一格。任意残留 queued/running/reconnecting/cancelling/unknown 也阻止后续 phase。

断连检查未实际触发、检查失败、--skip-disconnect-check、资源采样缺失、源码中途变化或任意错误，均不能取得完整验收。跳过断连只用于诊断。

### Gateway Session 微基准

scripts/benchmark_gateway.py 单独比较 Session 调度与传输，不混入 Hub/Agent 测量：
- before：显式 --before-module 指向本地审阅快照 .work/performance-baseline/gateway/remote.py，强制 SHA256 141d115d0b9fd636a6694ea2047a7acfb9181d47a3770331da23dd52bb8dc989
- 本地 .work 快照不随公开源码包发布，CI/单测不导入它。before 未显式给出文件或文件缺失时直接拒绝，不承诺开箱即有旧模块；须事先准备并核对上述完整 SHA。
- after：hub/gateway/remote.py
- 两者经 importlib 加载，使用同一 checkout 的网络/目录/协议依赖。因此这是单模块对照，不能称为完整旧版对照；manifest 明确记录这一点
- --transport mock：httpx.MockTransport，仅用于调度、状态和测量器检查；没有 socket，不据此宣称真实网络性能收益
- --transport loopback：同一合成 HTTP/1.1 keep-alive 服务，绑定 127.0.0.1；记录连接编号、复用数量和后端并发峰值。没有 TLS、公网延迟或真实 SaaS 后端
- modern 是明确无 session header 的现代 stateless 后端；当前默认上限 8。legacy 返回合成 session，保持串行 1。旧模块两种协议都串行
- 同样覆盖 8/16/32/64/128 及三种 workload；read/write 是合成内存计数，不冒充仓库磁盘读写
- queue_ms 使用两个实现共同的 before_send 回调，从调用至授权回调前的单调时间；warmup 不计入。RPC 本身的原始结果编号须匹配
- 每个 phase 后独立取消已发送的 -2 请求，等待后端排空，再发送不同编号 -3 作后续可用性检查。原 -2 永不重放；backend.jsonl 保留两者原始状态。mock 的取消会中止 in-process handler，真实 HTTP 则可能继续完成，二者不能混称
- 独立账户、grant、binding/account/catalog/connector version 的隔离、排队后的重新授权属于 tests/test_gateway_concurrency.py 的功能回归。基准不改变这些生产语义，也不将 Session fixture 冒充完整 IAM 证明

## 本地低负载检查

下述命令只执行指定纯 mock/helper 测试，不启动真实 listener/Hub/Agent。将 PY 指向已安装依赖的解释器：

    PY=/path/to/existing/python
    "$PY" -m pytest -q tests/test_benchmark_hub_agent.py tests/test_benchmark_gateway.py
    "$PY" -m ruff check scripts/benchmark_hub_agent.py scripts/benchmark_gateway.py tests/test_benchmark_hub_agent.py tests/test_benchmark_gateway.py

测试不从 test_*.py 导入共享 fixture。新增测量器失败、超时、状态错误和脱敏行为都有独立负向检查。秒级 mock 结果只证明脚本语义，不能替代下面真实矩阵。

## 低负载窗口内的正式命令模板

以下是准备好的命令模板，不表示已执行。先冻结所有被测源码和两个 harness，确认没有旧回归或延迟敏感生产工作争用。所有命令前台串行运行；每次输出必须用新目录。

    PY=/path/to/existing/python
    AFTER=/path/to/codepier-after
    BEFORE=/path/to/codepier-before
    cd "$AFTER"

    "$PY" "$AFTER/scripts/benchmark_hub_agent.py" \
      --repository "$BEFORE" --variant before --output .work/hub-pair01-before \
      --operations-per-worker 32 --host-context "实际已核实的同时运行活动"
    "$PY" "$AFTER/scripts/benchmark_hub_agent.py" \
      --repository "$AFTER" --variant after --output .work/hub-pair01-after \
      --operations-per-worker 32 --host-context "实际已核实的同时运行活动"

下一对反转次序 after → before。至少三个 ABBA 循环（六个相邻配对），保留全部结果，不选最好的一次。初次仅可先跑 --concurrency 1 --operations-per-worker 1 作真实 harness 冒烟，明确标成 smoke，不并入正式矩阵。

Gateway 两个变体都指同一当前 checkout；--variant 决定精确加载的模块：

    "$PY" "$AFTER/scripts/benchmark_gateway.py" \
      --repository "$AFTER" --variant before --transport loopback \
      --before-module "$AFTER/.work/performance-baseline/gateway/remote.py" \
      --output .work/gateway-pair01-before --operations-per-worker 32 \
      --host-context "实际已核实的同时运行活动"
    "$PY" "$AFTER/scripts/benchmark_gateway.py" \
      --repository "$AFTER" --variant after --transport loopback \
      --output .work/gateway-pair01-after --operations-per-worker 32 \
      --host-context "实际已核实的同时运行活动"

同样交错次序。modern/legacy 分开比较，mock/loopback 永远不合并。--short-seconds 默认 .02、--long-seconds 默认 .2；任何调整必须前后相同并保留在 manifest。合成耗时本身限制吞吐，上限不是生产服务容量。

## 原始证据与统计口径

每次根目录：
- manifest.json：完整 commit、dirty 标志、tracked diff + 全部非忽略 untracked 文件内容的哈希、harness hash、Python/platform/CPU 数、预检与结束 load、已知并发活动、开始/结束时刻和最终状态
- summary.json：每个协议/workload/并发格的汇总
- 每格 samples.jsonl、resources.jsonl、summary.json；Gateway 另有 backend.jsonl
- Hub 样本以完成顺序及时落盘，结束后补充数据库 event 证据。若进程被强杀或启动失败，没有完整 summary 的目录明确属于未完成，不能补成成功

统计：
- nearest-rank P50/P95/P99 同时报告全样本和成功样本，错误不会被过滤掉。成功吞吐是成功数除整个 phase wall time
- by_kind 必须看短请求，不能用长任务占比变化掩盖短请求尾延迟
- Jain 指标是 worker 平均时延倒数的公平性，不是固定窗口每 worker 吞吐；每个 worker 的 raw 可重算。长短 worker 工作量不同，优先同类/同混合比例比较
- 闭环固定 worker 压力，不做 coordinated-omission 修正，不声称固定到达率 SLA 或饱和容量
- 每格默认 4 次/worker 只是 smoke 级样本量；正式用 32 并做重复。任一 kind 的 P99 若不足 1000 成功样本，应标为低样本/探索性，不作为达标证据
- 采样 ps 仅使用自有 fixture PID，每 0.5 秒一次，三秒内未返回则记采样失败。CPU 是 ps 生命周期平均百分比，不是严格区间 CPU；RSS 单位 KiB
- Hub CPU/RSS 是两个父进程，不含完整子进程树；child CPU 是本次 fixture 生命周期内已回收子进程用时差，包含 fixture 启停及采样开销，maxrss 是平台原生单位的累计峰值
- Gateway CPU 是本进程区间 resource 差，包含合成 HTTP 服务；RSS/ps CPU 也同时含测量器。它不等同部署 Gateway 独立进程占用
- 同一 harness/interpreter、warmup、采样频率和 JSONL 开销适用于前后两侧。不同 harness hash 的结果不得配对

## 噪声、停止与达标规则

先比较完整性，再比较性能：
1. 全矩阵存在、源码前后 hash 不变、没有未终结 operation、没有重放/重复、取消/断连验证实际执行且通过、错误率为零。
2. 原始文件都可解析；队列与资源缺失不能填零。明确区分不适用（如 Gateway 没有 Agent queue）和缺失测量。
3. 同一相邻配对的持续 load/CPU 数不应相差超过 0.2；phase 期间一方超过 1 或已知争用活动不同，整对标为 noisy。即使都低于阈值也附原始 load，不能自动称为安静。
4. 同变体同格跨重复 P95 或吞吐相对中位数波动超过 20%，标为不稳定，不给达标结论。保留该次，不“重跑直到绿色”。另约合适窗口后开始新的一整对。
5. P95 降幅 = 1 - after/before；吞吐倍数 = after/before。只在同协议/传输/workload/并发格上计算，再报告六个配对的中位数、范围及原始各值。不得跨 8–128 并发合成一个漂亮数字。
6. “P95 -50%、吞吐 2x”必须明确哪些格和短请求子集满足，哪些不满足/样本不足；legacy 预期保留串行，不能以降低隔离换得收益。
7. 预检阻塞、失败/异常状态、未完成 phase 都有非零退出；不要继续用同 fixture 跑下一格，不自动重放未知操作。

真实矩阵、生产网络、跨账户功能回归、完整发布矩阵、提交、推送、发布、部署分别记录。P2 性能测试不要求部署；准备脚本和本地通过也不授权发布。
