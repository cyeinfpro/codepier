# 验证工具索引

日常修改先运行相关 pytest 模块、Ruff/mypy、生成物检查；PR 自动跑轻量检查，全量多平台 CI 仍由 workflow_dispatch 选择。下表是手工平台验收入口，未被默认 CI 调用不等于可删除。不要在无相应平台或夹具时把模拟结果报告为真实平台通过。

| 脚本 | 用途与前提 |
| --- | --- |
| [scripts/browser_check.py](../scripts/browser_check.py) | 真实面板和隔离 Hub/Agent；需 Playwright Chromium |
| [scripts/capture_chat_window.py](../scripts/capture_chat_window.py) | 合成原生聊天数据的截图；无真实 Hub 或模型调用 |
| [scripts/ui_unification_capture.py](../scripts/ui_unification_capture.py) | 隔离 UI 布局矩阵截图 |
| [scripts/ui_comfort_audit.py](../scripts/ui_comfort_audit.py) | 隔离几何与文字对比度审查 |
| [scripts/check_lsp_real.py](../scripts/check_lsp_real.py) | 预装 Pyright 的真实 LSP 往返 |
| [scripts/check_mcp_compat.py](../scripts/check_mcp_compat.py) | 官方 MCP SDK 互操作；自动回归由 tests/test_v140_compatibility.py 调用 |
| [scripts/check_native_chat_real.py](../scripts/check_native_chat_real.py) | macOS 真实原生 CLI 的基础聊天与续接 |
| [scripts/check_native_chat_complete_real.py](../scripts/check_native_chat_complete_real.py) | macOS 真实原生 CLI 的模型、思考档位切换与完整续接 |
| [scripts/check_native_worker_real.py](../scripts/check_native_worker_real.py) | 真实 Worker → Pi/Codex → localhost 图像协议 |
| [scripts/check_native_windows.py](../scripts/check_native_windows.py) | Windows ConPTY 与 Job 回收，仅回显子进程 |
| [scripts/check_codepier_migration_macos.py](../scripts/check_codepier_migration_macos.py) | 独立临时 launchd 服务的品牌迁移与回滚 |
| [scripts/check_codepier_migration_docker.py](../scripts/check_codepier_migration_docker.py) | 唯一命名 Docker 数据与 HTTPS 网络迁移夹具 |
| [scripts/check_codepier_proxy_docker.py](../scripts/check_codepier_proxy_docker.py) | 唯一命名 Docker 网络的网关迁移 |
| [scripts/rehearse_host_engine.py](../scripts/rehearse_host_engine.py) | 合成 Hub 状态和独立 Docker 镜像的宿主更新演练 |
| [scripts/check_ci_promotion.py](../scripts/check_ci_promotion.py) | 只读复用同一提交的完整 CI 证据；证据不足不晋升 |

在仓库根目录以 `python -m scripts.<名称> --help` 查看参数；有些截图工具直接按模块 docstring 运行。原生聊天验收要求预装 CLI，使用独立 HOME、localhost 模型夹具和已证明的外网拒绝；不继承真实模型凭据。Docker/launchd 工具只管理自己创建的唯一命名夹具，不代表获准更新现有服务。输出放在 `.work/` 的本轮目录，日志不加入公共源码包。

基础和完整聊天验收共用 `scripts/native_chat_fixture_support.py`，保留各自场景。宿主最低 Python 3.9 用 `scripts/check_host_python.py --smoke` 检查；Python 应用使用 3.13。

## 构建与兼容边界

`web/public-assets.json` 声明路由资源和下载入口；`scripts/build_web_assets.py` 为初始资源和延迟资源统一生成内容哈希。生产 `/static` 只服务生成清单内文件。生成的 JS/CSS/HTML/ZIP 仍随源码发布，以便旧版更新器无需 Node 即可安装；GitHub 通过 `.gitattributes` 折叠生成物。

`shared/brand_migration.py` 是服务归属判断源；安装和生命周期脚本必须脱离 runtime 运行，副本由 `scripts/sync_standalone_helpers.py` 同步并以 `--check` 校验。`shared/release_source.py` 是发布下载协议源，宿主更新器保留独立生成副本。

[subscription_lifetime.py](examples/subscription_lifetime.py) 是不会创建订阅的协议决策参考，[dot_outbox.py](examples/dot_outbox.py) 是外部宿主可选适配器。二者位于文档示例目录，不再进入 Agent/Hub 运行模块；对应契约测试保留。`JoinResult` 是对外响应契约，HTTP/服务测试用它校验实际返回数据，保留这类测试契约不增加生产序列化开销。

四个 relay 旧入口仍供历史启动器使用；没有所有已安装节点完成迁移的证据，不能仅凭仓库内部无引用就移除。两个 scripts 别名直接指向规范实现，避免别名链。
