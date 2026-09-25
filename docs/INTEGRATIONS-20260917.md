# CodePier 开发能力 · 安装与使用

本版为现有 Hub / Agent 的增量能力，不是另一套执行器。面板入口为「开发能力」。源码已写入不代表你的线上 Hub、Agent、ChatGPT 工具缓存或浏览器扩展已经更新；运行就绪页分别显示这些状态。

就绪检查分别展示本机配置、当前凭据权限和暂停状态。项目本身可写，不代表只读凭据可以写入；浏览器消息桥已经运行，也不代表本机控制入口已启用。程序缺失、权限未知、未实测和暂停都有独立状态。并发重叠的工具调用不会计算成串行间隔，切换项目映射或设备后也不会拼接旧计时。

## 能力与入口

| 功能 | 使用入口 | 完整行为与边界 |
| --- | --- | --- |
| 双版本 MCP | 原 `/mcp` 或 `/mcp?profile=coding` | 2025 三版握手与 2026-07-28 逐请求协议共用权限及持久操作；不宣称实现未发布的可选扩展 |
| 聊天内项目与改动卡片 | `open_workspace` / `show_changes` | 两个离线自包含 MCP Apps 资源；不支持 Apps 的宿主仍返回文字；旧卡片按固定 `review_ref` 读取 |
| 原生附件导入 | `download_artifact` / 项目卡片 | 宿主文件对象、可信 HTTPS 源、流式大小与 SHA 检查、禁止覆盖、禁止自动解压/执行 |
| 语义代码导航 | 开发能力 → 代码导航 | 文件/项目符号、定义、引用、悬停、诊断、调用方/被调用方；调用本机明确配置的语言服务 |
| 隔离工作目录 | 开发能力 → 工作目录 | 真正的 Git worktree；每次返回固定工作目录编号；编辑、搜索、产物与验收沿用这个编号 |
| 版本绑定验收 | 开发能力 → 验收 | 执行真实命令并保存前后源码指纹；变更后显示过期，覆盖不完整不能显示通过 |
| 人工接受验收 | 验收详情 | 仅管理面板主理人可接受或拒绝；MCP 不能接受自己的工作 |
| 继续任务 | 开发能力 → 继续任务 | 原目标、断点、未解决问题、证据与结果不明的原操作；不自动重放写操作 |
| 调用诊断 | 开发能力 → 运行状态 | 服务耗时、有效调用间隔、重叠与未知情况；间隔不是模型思考时间 |
| 暂停 / 恢复 / 紧急停止 | 运行状态 / 本机控制脚本 | 暂停新 MCP 修改；保留读取及回执；停止仅处理有归属证据的进程，不关闭 Agent |
| 独立后台浏览器 | 开发能力 → 后台浏览器 | Chrome 扩展 + 原生消息宿主，不启动 Codex；显式绑定浏览器档案、项目和网站 |

## 升级原则

使用现有的 Hub / Agent 升级流程，先备份、检查活跃任务、确认新运行时，再切换。不要覆盖旧配置、认证、技能或原生模型设置。未完成原生会话继续使用其已有 worker；这次源码交付没有重启正在使用的服务。

Hub 部署需要包含 `hub/`、`shared/` 和完整 `web/`。Agent 需要包含新的 `agent/`、`shared/`。安装与本机控制的正式入口位于 `agent/`，可由旧版本的受管 ZIP 白名单接受；源码目录中的同名 `scripts/` 仅作为兼容入口。发布前运行 `python scripts/build_integration_assets.py`，以同一份源码生成扩展下载包和本页的静态副本。Apps HTML 已随源码分发，修改卡片源代码后再在 `web/mcp-apps` 内运行 `npm ci --ignore-scripts && npm run build`。

## 1. 原生附件

先打开项目，再把当前宿主提供的原生 `file` 值交给 `download_artifact`，同时提供 `project`、未使用的相对 `path` 和稳定 `idempotency_key`。隔离目录需要原 `workspace_id`。

不要自行拼接下载 URL、复制 cookies 或把大文件转换成工具文本。宿主未提供原生文件输入时明确不可用，不能伪造成功。默认可信源为 `files.oaiusercontent.com` 和 `cdn.openai.com`；如宿主变更下载域名，由本机主理人核实后添加准确域名，不允许通配整个互联网。默认上限 128 MiB，可本机调整至最多 512 MiB。

请求已保存时继续查询原 `operation_id`；下载失败不会把半文件发布为成品。文件存在时不会覆盖。无宿主文件选择 API 的 Apps 卡片保留文字工作流。

## 2. 配置真实语言服务

先在目标 Agent 电脑安装需要的语言服务。本程序不会根据模型请求自动安装或执行一个陌生服务。示例使用已经安装的 Pyright；推荐填写可执行文件的绝对路径，避免启动服务时 PATH 与终端不同。

将以下片段保存为自己的 JSON 文件（不要替换完整 Agent 配置）：

```json
{
  "language_servers": {
    "python": {
      "command": ["/absolute/path/to/pyright-langserver", "--stdio"],
      "projects": ["Imago"],
      "timeout_seconds": 20
    }
  }
}
```

在已升级的 Agent 运行目录执行：

```sh
python -m agent.setup_integrations --config "$HOME/.codepier-agent/config.json" configure --settings /path/to/codepier-integrations-example.json
```

命令默认只预览。核对后加 `--apply` 才写入，自动保留原配置备份。脚本只合并 `integrations`，不扩大已有项目目录、Shell、MCP、浏览器或模型权限。正常重启 Agent 后启用变更。

查询需要项目与本机执行许可。`lsp_status` 只探测程序是否存在；真正查询才启动服务、协商协议并读取结果。每次查询结束、超时或取消都回收其进程组。返回一基行列，列单位为 Unicode 字符，内部正确转换 UTF-16；项目外位置不会返回。语言服务诊断不是测试运行，超时和未收到诊断不能伪装为空错误列表。跨文件引用、项目符号和调用关系先等待一次当前源码分析的正向协议回执，避免冷索引漏结果；缺少就绪回执时明确返回 `LSP_NOT_READY`。未指定路径的项目符号查询会有界发现授权源码作为初始化依据，返回 `index_seed`；这仍不代表整个仓库的原子快照或全覆盖证明。

## 3. Git 隔离目录

需要已有提交的 Git 项目。默认目录为源项目旁的 `CodePier-Worktrees`；该路径必须已经位于本机允许写入的范围。仅授权源项目而未授权旁边目录时，应由主理人配置合法位置，不能自动扩大授权。

创建时固定精确提交，不复制源目录未提交改动，不初始化 Git，不提交、不合并。工具返回的 `workspace_id` 不是凭据；后续每次访问重新核对项目、设备、调用者和目录身份。

进入编辑后，所有文件修改、移动、删除、搜索和产物都作用于选定 worktree。移除只接受已确认编号且完全干净、无活动任务的受管目录，拒绝强制删除。当前 MCP 源码目录若没有 `.git`，此功能会明确说明不适用，不擅自初始化。

## 4. 运行与验收

「运行验收」对选定工作目录执行命令，并绑定执行前后的源码指纹。验收 cwd 必须位于该目录；普通 Shell 的已授权完整能力不受这项限制影响。命令本身仍以 Agent 系统账号权限运行，这不是 OS 沙箱。

状态分别为通过、失败、源码已变化、覆盖尚未核实及执行中断。命令退出码为零是必要证据，不等于业务正确；由人检查命令输出和改动再接受。文件采样有明确范围、预算和排除项，不是原子文件系统快照，也不把不同会话同时发生的改动都归给一个会话。

验收列表只展示历史状态；详情重新计算当前指纹。接受后又发生源码变化，接受标记也失效。记录中的操作编号可在原操作审计查看输出，不会重新执行测试。

## 5. 后台浏览器安装

此功能可选，使用现有浏览器的登录状态，因此必须由本机主理人启用。没有授权不启动 Chrome，也不代替主理人确认扩展权限。

1. 在面板「本机配置」下载扩展源码包，解压到长期保留的目录。
2. 在 Chrome 扩展管理中开启开发者模式并「加载已解压的扩展」。打开扩展窗口，记录扩展编号与档案编号。
3. 在本机执行安装预览，填写准确项目与网站 origin：

```sh
python -m agent.install_browser_bridge --config "$HOME/.codepier-agent/config.json" --extension-id YOUR_EXTENSION_ID --profile-id YOUR_PROFILE_ID --project Imago --origin https://example.com
```

核对后加 `--apply`。安装器保留配置备份和文件归属回执；不复制浏览器账号、不修改模型设置、不自动重启服务。Chrome、Chromium、Chrome for Testing 可通过 `--browser` 指定。自定义用户数据根目录使用 `--profile-root`。

Windows 原生消息宿主必须是原生 EXE，而不是伪装的 `.cmd`。在 Windows 源码环境用 `scripts/build_browser_host.py` 构建，再以 `--native-executable` 明确指定；构建依赖及参数见脚本 `--help`。不同平台是否完成真实验收，以本轮验收报告为准，不能用构建成功替代真实登录与控制流程。

4. 正常重启 Agent 后，在扩展窗口授权同一个网站。Agent 与扩展的准确 origin 白名单必须同时匹配，端口不同属于不同 origin。
5. 保持 Chrome 自然处于前台，在扩展点击「准备标签页」，创建空闲池。远程调用只能租用这些已有后台页，不创建或激活窗口；空池时明确提示。
6. 使用面板打开、观察、填写、点击、选择、滚动、导航键或同站授权范围的跳转。每次输入必须使用最新观察编号；输入后需要重新观察。动作回执仅代表 DOM 操作已派发，不是网站业务提交成功，也不是可信 OS 按键。

用户选中或移动标签页后，远程操作主动让出控制。租约过期回收；重复请求不重复点击。未确认输入返回需要核查，不会重新发送。网站脚本自身的行为不属于浏览器扩展可证明的业务结果，重要提交仍需用户明确确认。

卸载仅移除安装回执中未被修改的受管文件和配置片段，保留浏览器档案、登录和其他设置：

```sh
python -m agent.install_browser_bridge --config "$HOME/.codepier-agent/config.json" --uninstall
# 核对预览后添加 --apply
```

## 6. 本机暂停与紧急停止

需要本机显式设置 `integrations.local_control: true`。监听器只绑定回环，拒绝网页 Origin、代理转发和错误 Host；随机凭据只在权限受限的私有状态目录中保存，不放进 URL 或输出。

```sh
python -m agent.codepier_control --descriptor "$HOME/.codepier-agent/state/integration-local.json" status
python -m agent.codepier_control --descriptor "$HOME/.codepier-agent/state/integration-local.json" pause --project Imago --confirm Imago
python -m agent.codepier_control --descriptor "$HOME/.codepier-agent/state/integration-local.json" resume --project Imago --confirm Imago
python -m agent.codepier_control --descriptor "$HOME/.codepier-agent/state/integration-local.json" stop --project Imago --confirm Imago
```

状态目录可能被本机配置改变，以安装器显示的 descriptor 路径为准。停止默认不动原生 Pi/Codex 会话；明确添加 `--include-native` 才包含本项目原生会话。报告区分取消请求、已确认停止和未确认项，不把请求发送成功当作进程已退出。失去回执时用脚本输出的原 `--idempotency-key` 恢复。

## 7. 权限与限制

原 MCP 禁止启动本地 Codex 的策略保留；新增 LSP、worktree 和验收执行也进入同一执行策略。管理面板原生会话仍属于管理员明确操作，不加入公开 MCP 工具。读取 Skills 不启动模型。

完整模型可见目录为现有工具加上新功能，`integration_control` 与 `validations_accept` 不向 MCP 发布。工具显示集不是授权；新能力仍分别检查 read/write/execute/computer。浏览器需额外本机 opt-in，不因为 OAuth 已有 computer 就自动开启。

保护的项目、文件和 URL 不会写入公开静态包。工作目录、验收和浏览器租约按调用者、项目映射、设备绑定；换凭据不会自动继承其他授权的私有记录。源码与权限检查不是任意代码的 OS 沙箱，不能承诺同系统账号全权限执行条件下无法绕过所有策略。

## 8. 发布验证

先运行新增回归，再运行完整测试、独立 MCP SDK 与实际浏览器。真实 Chrome 测试使用隔离档案和本地测试站点，不使用私人账号。真实语言服务验收与协议替身测试分开标注。

完整回归可以按模块使用独立进程，避免某个浏览器测试卡住后整轮没有结论：

```sh
.venv/bin/python scripts/check_full_regression.py --output docs/evidence/my-new-verification --workers 1 --timeout 900
```

默认逐模块串行运行，避免真实浏览器争抢本机资源；需要并行时可显式配置 workers。脚本先收集全部测试编号，逐模块保存输出、退出码和 JUnit，再核对每项 setup / call / teardown。失败、跳过、缺失、超时和测试期间源码变化都不能算通过；不会自动重试或排除失败项。请使用新的输出目录保留历史证据。独立 SDK 环境存在时自动使用 `.venv-compat/bin/python`，也可明确设置 `MCP_COMPAT_PYTHON`。

每次验证应分别记录结果、未执行的平台或宿主验收、源码摘要与操作编号；历史本机验证资料不随公开源码分发。发布条件见 [发布清单](../docs/RELEASING.md)，不能把其他日期的成功记录当作当前版本的验收。

## 参考与许可

实现适配 CodePier 的现有 Python/SQLite/权限/幂等体系，没有把参考项目作为额外运行服务。参考：WebCodex（工具契约、语义导航和诊断）、DevSpace（Apps、原生附件、worktree）、C2C（交接与独立复核）、Mac Developer Bridge（后台浏览器与本机控制）、Codex ChatGPT Web（隔离配置及发布门槛）。

上游链接：
- https://github.com/yyjeqhc/webcodex
- https://github.com/Waishnav/devspace
- https://github.com/XiaoDuoYa/codex-with-chatgpt
- https://github.com/alexanderradahl/mac-developer-bridge
- https://github.com/miuuyy/codex-chatgpt-web
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://developers.openai.com/plugins/reference
- https://github.com/microsoft/pyright

Apps 打包使用锁定的 `@modelcontextprotocol/ext-apps` 与其依赖，许可证由构建脚本收集到 `web/mcp-apps/THIRD_PARTY_NOTICES.txt`。扩展与 Python 集成为本项目实现。不要把“零风险”、官方支持或通过全部跨平台实机验收作为没有证据的宣传。
