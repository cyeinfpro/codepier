# MCP 热点设备投递对照

2026-11-18 在隔离的临时 SQLite 库上分别验证 1 台及 2 台设备。每种规模只改变热设备的 `HUB_DELIVERY_ROLLING_DEVICES`，全局槽为 8，每设备预取为 2，滚动补位的批量为 16。夹具使用实际候选查询、持久队列和投递流水线，准备及发送使用合成包，按发送顺序计算等待。

热设备初始有 140 条到期请求，其中第 2 条是写请求。每送出 8 条中的第一条时加入一个新读，累计 18 条。热设备首包开始发送后，两条更早创建的 cancel 和 probe 转为到期。双设备场景此时在冷设备加入一个短读，并把热设备首包暂停到冷设备短读发出。

| 设备数 | 热设备策略 | 首轮选中数 | cancel / probe 在热设备的投递位次 | 冷读在全局的投递位次 | 唯一投递数 |
| ---: | --- | ---: | --- | --- | ---: |
| 1 | 原批次 | 128 | 129 / 130 | — | 160 |
| 1 | 滚动补位 | 16 | 17 / 18 | — | 160 |
| 2 | 原批次 | 128 | 129 / 130 | 2 | 161 |
| 2 | 滚动补位 | 16 | 17 / 18 | 2 | 161 |

两条控制请求各少等待 112 条热设备投递。每格都核对了原有读写顺序、新读顺序、同时间戳的 `rowid` 顺序、操作 ID 唯一性以及退出后 8 个槽位全部释放。此项统计是夹具内的投递位次，短读 P95 和吞吐由真实 Hub/Agent 基准另行测量。

同路径另有真实 Agent 磁盘回归。两个排队写请求使用相同旧 SHA 和不同幂等键，先到写者提交成功，后到写者返回 `SHA_CONFLICT`，随后到达的读者和最终文件都保留第一次更新，只为此次成功写入生成一份备份。既有回归同时检查较早写者不被晚到读者越过，以及取消后资源可重新使用。

```sh
.venv/bin/pytest -q -s tests/test_delivery_hot_backlog.py tests/test_conflicting_sha_writes.py \
  tests/test_agentdock_scheduler.py::test_queued_writers_finish_fifo_after_shared_reader \
  tests/test_joint_resource_admission.py::test_conflicting_writer_keeps_fifo_while_capacity_blocked_and_cancel_unblocks_read
```

结果为 `8 passed in 2.67s`。
