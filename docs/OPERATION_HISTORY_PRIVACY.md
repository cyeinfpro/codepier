# 操作历史的正文存储与保留

新完成的操作结果和输出尾部使用 Hub 密钥加密持久化。运行中的输出保留原有字符上限，正文位于单独加密列；轮询字符数和序号不依赖解密全文。MCP Tasks 冻结结果单独加密，禁止修改已冻结的正文；密钥轮转仅允许认证解密后内容完全相同的重加密。

## 显式维护，不在升级时清理真实数据

hub.operation_history.prune_history(store, now=..., batch=100) 默认 dry-run。只有显式传 dry_run=False 才执行一次有界维护事务。默认终态正文保留 30 天，Hub/协作审计保留 365 天。dry-run 和实跑使用同一筛选条件，均返回操作、审计和协作候选计数。batch 分别限制操作数与 Hub 审计数；协作各表共享一个 batch 总额。没有启动钩子、周期调度或生产库操作。本次修复仅在合成临时数据库测试，生产启用需另行安排。

操作只清除无引用的 succeeded/failed/cancelled 正文和尾部，写入加密的 HISTORY_EXPIRED 回执。操作 ID、状态、时间、幂等键、指纹不变，不会因过期而重新执行旧请求。未决、unknown、needs_review、运行中记录始终保留。审计清理也保留与未决/受引用操作关联的记录。

冻结 MCP Tasks、协调操作、工件、有效活动订阅与事件 outbox 均视为引用，排除清理。返回 protected_by_reference 数量和 reference_reasons 原因类别。MCP Tasks 当前没有自动到期释放机制，所以其正文可长期受保护；不能宣称所有终态正文都会在 30 天后删除。Tasks 的不可变回执和重放优先于节省空间。

协作清理委托 hub.collaboration.history_retention.prune_collaboration_history，保留身份、幂等墓碑和游标依赖，只处理确认终态且不再被使用的正文。未决投递、落后游标、活跃订阅与仍需重放的回执不删除。过期幂等回执返回明确的过期错误，不重新执行。

## 旧明文无损迁移

hub.operation_history.encrypt_legacy_history(store, batch=100) 是显式、可重复执行的有界迁移。迁移 operations 结果/输出、旧 live tails、冻结 terminal_result 和整个旧 terminal_operation；每列每批最多 batch 条。旧快照只有认证后明文完全相等才可重加密，状态与正文的修改仍被触发器拒绝。迁移保留既有截图/桌面文本短期 TTL 元数据；不把其延长到 30 天。升级不会自动运行此迁移。

旧记录保持可读；旧备份、数据库空闲页和 WAL 不会因为逻辑列加密或清理而自动物理擦除。必须保护备份和匹配密钥，并在另行授权的维护流程处理备份保留及物理介质。日志、审计元数据、错误摘要不在正文加密范围内。

冻结快照的持久触发器只依赖 SQLite 内置表达式，普通离线 SQLite 状态维护不依赖连接私有 UDF。显式无损重加密先验证认证明文相同，再在已有写事务和 savepoint 内暂设仅匹配该 task_id、原值与新值的精确例外，完成前恢复严格触发器；失败和进程崩溃一起回滚数据与触发器。
