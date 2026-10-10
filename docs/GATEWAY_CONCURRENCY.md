# Gateway 有界并发与连接生命周期

## 范围与默认值

Gateway 的业务准入上限仍为 32 次调用，完整调用仍由 45 秒超时约束。
连接池仍限制最多 64 个隔离 Session，空闲回收默认 300 秒，DNS pin 最长 900 秒。
本改动没有调整生产配置或加入依赖，没有给写工具添加自动重试。

每个 Session 保留一个长寿命 HTTPX AsyncClient，最多复用 8 条连接。
只有成功完成现代协议 server/discover 且没有返回会话标识时，工具调用才能同时占用最多 8 个租约。
未知协议的初次协商独占；legacy 及返回会话标识的 modern 后端保持串行。
这不是 legacy 会话池，不声称支持跨 legacy 会话的并行语义。
构造参数 concurrency 可用于受控测试，范围 1–32，不增加对外运行时配置入口。

## 准入、鉴权和隔离

Session 使用 FIFO 队列和条件变量，锁仅保护初始化、准入和生命周期。
实际 modern 工具 RPC 不占用整把会话锁。
队列中的取消会移除对应等待项，不占用槽位，也不执行 before_send。
before_send 在拿到租约且完成协商后执行；该回调继续执行真实 IAM/绑定/帐户/连接/工具检查。
回调失败或取消只归还自己的租约，不关闭其他正在执行的请求。

call key 包含用途、Space、用户、grant、binding id/version、account id/version、
account catalog hash、connector id/version。
目录变化也加入发送前签名比较，避免排队调用绕过新目录状态。
权限不由连接复用代替：每个调用都会重新验权。协议能力仅属于当前 Session 协商代际。

工具发现保持只读。整段 tools/list 分页持有独占租约，游标不会与同一 Session
上的工具调用交叉；等待中的发现按 FIFO 排在后来调用之前，防止持续调用使其饥饿。
服务层 discovery 使用独立用途 key，不与 call key 共享 Session。

## DNS、失败与关闭

到期或传输失败将挡住新租约，等待所有 active 租约结束后才关闭旧 client、重新 pin DNS。
旧请求不会因为另一个请求失败、取消或 DNS 到期而被关闭。
只允许后来独立授权的请求重新连接；任何已发送请求都不自动重发。
429、鉴权失败、重定向、5xx 等保持原错误，不触发重试。

取消一个已经进入 RPC 的请求会将该传输代际标记为 invalid；
其他 active 请求可以结束，新请求需等待旧代际排空。
关闭立即停止准入并拒绝尚未获得租约的调用，默认最多等待 25 秒。
若 active 请求尚未完成，关闭返回而不强制中断传输，最后一个租约负责清理。
关闭期间仍在进行的初始化也不得发送 tools/call。
连接池在关闭 I/O 和排空期间不持有全局池锁；关闭后的池不能重新接纳请求。

## 回执与性能观测

gwc_ durable receipts、request_key 指纹、重复请求恢复和 unknown 状态规则不变。
发送前拒绝落为 rejected/not_sent，已发送且没有可确认结果落为 unknown，不能据此重放。

Session.stats 仅累计 admitted、queued_seconds、active_seconds、peak_active，
不保存请求参数、密钥或身份。queued_seconds 包括协商及租约等待；
active_seconds 包括 before_send 和 RPC。累计值不能直接当作 p95/p99。

同机比较必须使用相同工作负载、相同 Python/依赖、相同并发度及相同 mock 延迟。
修改前已备份：
- .work/performance-baseline/gateway/remote.py
  SHA-256: 141d115d0b9fd636a6694ea2047a7acfb9181d47a3770331da23dd52bb8dc989
- .work/performance-baseline/gateway/service.py
  SHA-256: f946546722b22285f67739648c0399bbcbdf389d25b52ce764ac482de2c13156

基准应覆盖 8/16/32/64/128 并发、纯读、读写混合、长短混合及多个身份公平性，
输出原始样本、p50/p95/p99、吞吐、错误、重复效果计数、CPU 和内存。
mock 正确性结果不能替代真实 TCP keepalive 证明，也不能称为生产性能实测。

## 验证

tests/test_gateway_concurrency.py 用 -m 'not integration' 选择无 socket、无子进程的确定性 mock 测试，
覆盖并发上限、FIFO、单次初始化和client复用、legacy保守路径、撤权与版本变化、
取消、异常、无重复发送、DNS代际排空、目录独占分页、关闭边界、
身份隔离以及真实内存 SQLite 回执状态。
另有显式标记 integration/slow 的本地 HTTP/1.1 用例，验证连接数量上限和后续批次 TCP keepalive 复用。
既有 test_mcp_gateway_protocol.py、test_gateway_dns_recovery_audit.py 和
test_mcp_gateway.py 仍需回归。完整集成/真实传输和性能验收应单独报告。
