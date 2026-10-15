# 安全测试清理

## 范围

CodePier 全回归入口 scripts/check_full_regression.py 自动为收集阶段和每个测试任务分配受管 scratch，JUnit、事件、摘要和有界日志保留在 evidence 中。测试成功、失败、取消、超时后，先确认所属进程组结束，再清理本次 scratch。清理失败会设置 cleanup_pending，不能算完整验证通过。

任意已映射项目可以显式使用 scripts/managed_test_run.py 包装测试命令。普通 exec、第三方测试自己写到项目目录的文件、旧工作树、虚拟环境、历史报告和备份不会被猜测删除。本变更不修改任何现有项目映射、权限或 Agent 配置。

当前实现需要 POSIX fd-relative/no-follow 和 waitid/WNOWAIT，支持已验证的 macOS 环境；Windows 会拒绝运行，不会退回不安全删除。Linux 尚需独立系统验收。测试子进程必须保持在继承的进程组中；daemon/setsid 逃逸不属于此包装器可证明回收的范围。

## 映射项目接入

使用已有 Python 及 CodePier 检出路径，在目标项目工作目录运行：

```sh
/path/to/python /path/to/codepier/scripts/managed_test_run.py \
  --output-root .codepier-test-runs --cwd . --timeout 900 \
  --project PROJECT_ID --workspace WORKSPACE_ID --task unit-tests \
  -- /path/to/python -m pytest -p no:cacheprovider
```

每次在 output-root 下创建唯一 evidence 目录。元数据 project/workspace/task 是诊断标签，不是权限或所有权凭据。使用 argv 直接执行，无隐式 shell 展开。

Python tempfile 会使用受管 TMPDIR；pytest 应用 -p no:cacheprovider 避免默认项目根缓存。针对显式 basetemp/cache/output 的测试框架，应调用 ManagedTestRun API 并将它们定向到 run.scratch；必要报告定向到 run.evidence。npm/build 等工具若忽略 TMPDIR/XDG_CACHE_HOME，需要按其配置显式定向，包装器不会删除它们写到外部的内容。

可在本机已有 Agent tasks 中显式增加一个项目限定任务（下列仅为示例，不自动写配置）：

```json
{
  "safe-tests": {
    "projects": ["PROJECT_ID"],
    "cwd": ".",
    "timeout": 1000,
    "description": "Run tests with owned temporary data and retained evidence",
    "command": [
      "/path/to/python", "/path/to/codepier/scripts/managed_test_run.py",
      "--output-root", ".codepier-test-runs", "--timeout", "900",
      "--project", "PROJECT_ID", "--task", "safe-tests",
      "--", "/path/to/python", "-m", "pytest", "-p", "no:cacheprovider"
    ]
  }
}
```

外层超时应大于包装器超时并留出停止/落盘时间。外层强杀无法执行 finally，必须查看恢复状态。新增具名任务配置是否生效需另行确认，不等于本代码已经启用所有映射项目。

## 配额与报告

默认最小空闲空间 256 MiB、单次 scratch 2 GiB、stdout/stderr 保留最多 8 MiB；CLI 可设置 --min-free-bytes、--max-scratch-bytes、--max-log-bytes。容量是采样准入/停止保护，不是操作系统硬配额。报告、覆盖率和用户测试直接写出的文件不在 scratch 字节上限内，必要证据保留供审查，不自动过期删除。

managed-run.json 提供 schema、run_id、路径/设备/inode/uid 身份、所属进程状态、预算、测试结果、cleanup_state 和 cleanup_pending。日志截断明确标注 log_truncated。输出目录不可复用，旧文件不被覆盖。

## 崩溃恢复

```sh
/path/to/python /path/to/codepier/scripts/managed_test_run.py \
  --recover /absolute/path/to/managed-run.json --dry-run
```

默认只检查。仅对自己创建的已确认停止 scratch，可显式传 --apply 恢复回收。恢复不向历史 PID 发停止信号。活跃/未知 PID、活跃锁、路径身份变化、根或元数据符号链接、未确认启动窗口、设备文件或不完整所有权证据都会阻止删除。部分清理后标记丢失时，仅已核实 inode 的空 quarantine 可完成删除；非空目录仍保留并要求人工检查，不能凭年龄猜测。

该版没有定时全盘扫描，也没有自动应用历史清理；恢复命令为明确、可审查的本次记录入口。

## 历史工作树

```sh
/path/to/python /path/to/codepier/scripts/preview_worktree_cleanup.py \
  --root /path/to/original/repo --max-entries 100 --file-limit 100000
```

只读输出 dirty/untracked、ignored 数量、HEAD 是否被本地/远程引用包含、目录身份和有界大小估计。candidate_for_review 仅表示可以继续人工核对，所有条目 delete_authorized=false。它没有删除功能，尚未证明任务归属、活跃使用或忽略文件价值。原项目、独有提交、脏文件以及未知情况均不能自动删除。
