# 多 Hub 扩展准备度

本批完成并发热点优化与可重复测量工具；未部署多 Hub，未迁移数据库或引入 Redis/PostgreSQL。当前形态不应直接以多个写 Hub 共享 SQLite 的方式宣称 active-active 可用。

## 当前进程边界

- Store 的数据库执行器为单进程单 worker，事务与 after_commit 语义承载授权、幂等、任务完成和事件 outbox。
- Runtime 的 Agent WebSocket、投递 wake、operation 等待者与状态通知均由所属 Hub 进程管理。
- Gateway Session/HTTP pool 在当前进程内，stateful session 和认证版本不能任意转移或跨账号复用。
- Events 与原生投递已有 durable ID、outbox、重试 fencing 和二次验权，但进程外所有权与分布式撤权传播尚未建立。
- Agent 的 durable journal 保留断线恢复及不重复执行保证。扩容不能绕开它，也不能将“负载均衡失败”转换为新业务 ID。

## 在启用 active-active 前必须满足

1. 明确共享状态模型。先用本批 DB worker 排队/阶段耗时与操作阶段轨迹证明瓶颈，评估查询合并、索引和只读连接；不得直接把 SQLite worker 增为 16 或更换数据库后声称性能解决。
2. 为每个 Agent connection / Gateway stateful session 指定唯一所有者、租约与 fencing epoch。旧 owner 迟到的发送、ACK、result 必须被拒绝或按原 ID 安全合并。
3. durable admission、幂等 fingerprint、结果冻结和 outbox 仍保持单次事务语义。取消只是请求，必须等待原执行端终态。
4. 当前授权、角色/profile/项目映射和凭据版本的失效跨节点立即可见。性能缓存不存长期授权结论；撤权后任何节点不能沿用旧结果。
5. operation 等待与输出 seq 可跨前端重连，通知仅作可丢失提示。回执读取仍是依据，不依赖进程内 Future 永远存在。
6. 无状态 HTTP 可在独立请求上分流；stateful transport 必须隔离并有恢复契约。不得跨用户、Space、grant、backend、binding version 共用会话。
7. 建立一致的配额与公平性。单节点增加不应乘法扩大用户授权、后端并发或设备安全上限。
8. 验证滚动重启、租约失效、数据库故障、网络分区、撤权传播、旧节点恢复和重复回调。在这些故障下证明原 ID 不重复执行、终态不倒退、事件不会被当成执行授权。

## 推荐推进顺序

先完成同机单 Hub 的 before/after 数据，确认 Gateway、投递、DB 阶段各自占比；随后用隔离测试实现并检验所有权/失效协议，再决定是否需要跨节点共享存储。公开 Hub OAuth 路线与官方 Secure MCP Tunnel 的私有入口分开评估。

官方 Tunnel 的部署约束也不能由多 Hub 绕过；具体版本配置与单活要求见 [ChatGPT 文档](CHATGPT.md)。此文是设计门槛，不是部署指令或已完成的扩容能力。
