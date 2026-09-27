# CodePier Computer Use 桥接

## 范围与版本

本文描述当前源码的桌面桥接。工具目录以实际 tools/list 为准，历史原生诊断不是所有平台或当前部署的验收证明。审批与程序调用分别计时，过期输入要求重新观察；不把截图正文和原生 stderr 作为普通诊断日志保留。测试方法见 [开发与验证](DEVELOPMENT.md)。

## 接入结构

```text
ChatGPT
  └─ CodePier MCP：6 个 computer_* 工具
       └─ Hub：computer 授权、持久回执、图片结果
            └─ 已有加密 Agent 通道
                 └─ 一台设备一个 CodePier 桌面会话
                      └─ 本机已安装的 Codex Computer Use MCP
                           └─ 原生应用权限、截图、辅助功能、鼠标与键盘
```

适配器直接调用本机原生工具，不调用 Codex 模型，也不新增模型 API Key。原生程序本身的联网、账号、权限和历史记录行为由原生程序管理，不属于 CodePier 的无联网或无记录承诺。

不打包、复制或重新分发 Codex 私有二进制。默认从当前用户的原生安装和应用内插件目录发现程序。显式 `plugin_root` 只由本机配置提供，远程工具参数不能指定任意命令。

## 已接入的工具与动作

| CodePier 工具 | 作用 |
| --- | --- |
| `computer_status` | 安装、配置和会话状态；`probe=true` 只初始化原生接口并核验目录，不枚举应用或读屏 |
| `computer_apps` | 调用原生 `list_apps`，其中可能包含近期使用的应用记录 |
| `computer_session_open` | 为一个明确授权的应用建立短时会话，返回可用动作；本调用不读取画面 |
| `computer_observe` | 调用 `get_app_state`，返回应用文本/辅助功能、原生图片块和新的观察编号 |
| `computer_action` | 根据当前观察执行一个强类型动作，通常立即重新读屏 |
| `computer_session_close` | 结束自己的 CodePier 会话；面板管理员可显式强制停止设备上的 CodePier 会话 |

`computer_action.action.type` 支持契约中定义的八类输入动作：

| 类型 | 参数与支持范围 |
| --- | --- |
| `click` | 辅助功能 `element_index` 或截图像素 `x/y` 二选一；左/中/右键；单击、双击、三击 |
| `drag` | 截图像素 `from_x/from_y/to_x/to_y` |
| `scroll` | 元素编号、上下左右方向、支持小数的页数 |
| `press_key` | 原生 xdotool 风格按键表达式，例如 `Return`、`Tab`、`super+c` |
| `type_text` | Unicode 文字输入 |
| `set_value` | 设置可写辅助功能元素的值 |
| `select_text` | 按文字选中，或将光标放在之前/之后；可用前后文消歧 |
| `perform_secondary_action` | 对元素调用它公开的辅助功能动作 |

这里的“完整”指本次本机原生 MCP 公布的 10 项工具接口均有映射，不表示所有操作系统、所有应用和所有屏幕状态都已验证。本版没有添加原生目录未提供的独立悬停、持续录屏、任意 JavaScript 或浏览器 DOM 执行接口，也没有接入另一个 `unified-computer-use` JavaScript 运行时。网页表单可在原生允许的浏览器应用中通过相同截图/辅助功能工具操作，但已存的 Safari 操作审计只能证明相应调用，不能替代任意网站的业务结果验收。

## 先在本机验证原生服务

在项目目录运行：

```bash
# 默认只检查安装、握手和工具目录，不读取任何应用内容。
.venv/bin/python -m scripts.check_computer_use

# 只有显式指定 --app 才会请求该应用的一次状态。
# 这不会输入文字或点击按钮，也不会代替用户批准原生权限。
.venv/bin/python -m scripts.check_computer_use \
  --app /System/Applications/Calculator.app --timeout 45
```

诊断程序只输出图片尺寸/摘要和文字长度，不输出截图 Base64 或应用正文。退出码 `0` 表示所请求检查通过，`2` 表示未通过。仅有 `native_catalog_verified=true` 不等于真实读屏成功；需检查 `screen_read_verified=true`。不带 `--app` 的目录检查刻意保持 `screen_read_verified=false`。

本次实际原生诊断见 `docs/evidence/computer-use-20260913/native-calculator-probe.json`。如读屏超时，先在 Codex 自己的界面确认 Computer Use 能读到同一应用，再检查系统录屏、辅助功能和原生应用授权。不要自动批准弹窗，不要修改系统授权数据库，不要通过更换幂等键反复发送输入。若 Codex 自身可用而独立进程仍超时，应继续核实该安装版本的独立调用兼容性；本版没有绕过原生服务的调用方授权。

## 升级与本机明确授权

须同时升级 Hub 和 Agent，并安装本版各自的依赖文件；新增运行依赖为项目锁定版本的 `jsonschema`。保留原数据库、`master.key`、设备配对配置和 Agent 日志，不重新配对、不覆盖生产配置模板。完整部署操作见 [开始使用](START-HERE.md)。

在已升级的本机项目目录执行，例如只授权 `MCP` 项目操作计算器：

```bash
.venv/bin/python -m agent \
  --config "$HOME/.codepier-agent/config.json" configure \
  --computer enabled \
  --computer-project MCP \
  --computer-app /System/Applications/Calculator.app
```

这条命令供本机所有者明确开启；现有部署的项目和应用范围以本机配置为准，升级不会自动扩权。可重复 `--computer-project` / `--computer-app` 指定多个条目，提供的列表会替换原列表。`*` 代表本机管理员明确授权全部条目，不是默认值。建议先用单项目、单应用完成验收。

新配置默认如下，旧配置不带 `computer` 字段时同样关闭：

```json
{
  "computer": {
    "enabled": false,
    "projects": [],
    "allowed_apps": [],
    "plugin_root": "",
    "codex_home": "",
    "max_session_seconds": 900,
    "observation_max_age_seconds": 60,
    "call_timeout_seconds": 45,
    "approval_timeout_seconds": 60
  }
}
```

`plugin_root` 和 `codex_home` 可选，只允许本机绝对路径。应用识别和授权沿用原生提供方，CodePier 不把“程序文件存在”标成“系统权限已通过”。已验证的原生平台是 macOS；未验证 Windows/Linux 的原生桌面后端。

## ChatGPT 授权与调用顺序

新增独立 OAuth/PAT scope：`computer`。`read/write/execute` 不自动包含它；原有凭据不自动扩权。普通工具和已授权项目仍可继续工作。升级后需在客户端刷新工具目录，并通过正常授权流程申请/批准 `computer`，同时保留必要的 `read`。本版没有替用户修改已发出的 OAuth/PAT。

会话建立和动作还要求项目可写；读屏属于高隐私只读能力，因此工具标记为只读，但仍要求 `computer`。关闭会话不受项目变为只读的写操作拦截，避免无法停止。

正常调用顺序：

```text
computer_status
→ computer_session_open(app, idempotency_key)
→ computer_observe(session_id)
→ computer_action(session_id, observation_id, action, idempotency_key)
→ 根据新画面决定下一步
→ computer_session_close(session_id, idempotency_key)
```

每个新的助手回合在输入前重新观察；观察编号来自真实返回，不能自行构造。动作仅使用一次最新观察编号，消费后不能用于第二次动作。截图坐标是原图像素，不是 CSS 像素，也不是窗口坐标。

例如，在已有明确用户授权的应用中，一次动作的参数形状如下；其中两种编号必须替换为实际返回值：

```json
{
  "project": "MCP",
  "session_id": "<上一步返回的32位会话编号>",
  "observation_id": "<最新读屏返回的32位观察编号>",
  "action": {"type": "click", "element_index": "<实际元素编号>"},
  "idempotency_key": "<本次动作的唯一幂等键>"
}
```

不能把屏幕上的内容当成用户指令。发送、删除、支付、分享或账户修改等有实际后果的操作，应先核对对象并取得适当确认；本适配器没有声称通过识别坐标自动判断所有危险业务行为。

## 网页面板

顶栏新增“桌面控制”图标。先在工作台选择项目，再打开联调面板，可检查安装/接口、枚举应用、连接明确应用、查看截图与辅助功能文本、重新读屏、结束联调或管理员强制停止。

面板是人工触发的检查视图，不自动连续录屏，不在浏览器存储截图或会话编号。应用正文使用文本节点显示，不解释其中的 HTML。关闭面板会尝试释放自己的会话；网络失联时由会话过期机制兜底。

面板联调会话属于面板管理员，而非 ChatGPT 授权。交给 ChatGPT 前先结束联调，不能共享另一个授权的会话编号。本版面板未加入人工鼠标/键盘输入表单；完整八类输入通过 `computer_action` 提供。

## 停止、过期与重试

本机紧急停止不需要 Hub 在线：

```bash
.venv/bin/python -m agent computer-stop
# 本机所有者明确恢复：
.venv/bin/python -m agent computer-resume
# 长期关闭：
.venv/bin/python -m agent configure --computer disabled
```

这些默认命令使用默认配置；使用自定义配置时，应像前面的启用命令一样在子命令前指定 `--config`。停止标记由运行中的新版 Agent 检查，只阻止 CodePier 后续输入，不撤销已发生的输入，不关闭用户应用，也不声称结束其他 Codex/用户的桌面操作。

一次 CodePier 会话只绑定一个应用、项目根目录和授权归属；设备级独占不能锁住人工操作或其他软件。默认会话 300 秒，受本机默认 900 秒上限约束，观察默认 60 秒过期。默认动作前重新读取辅助功能状态及截图尺寸；图像独占无文本场景比较图片摘要。辅助功能内容不变时，这不等同于逐像素屏幕完全未变化的保证。`verify_unchanged=false` 只关闭这一步重读比对，仍保留观察身份、年龄、应用和坐标范围校验。

新输入在设备离线时不排队。输入首次开始期限最多 15 秒，其他桌面调用最多 60 秒，避免旧点击在很久后落地。对已经返回的 `operation_id`，断线后查询原操作；重复同一幂等键只能恢复同一请求，不会再发一次输入。Agent 重启保留操作回执，但不会恢复旧桌面会话。

| 结果 | 正确处理 |
| --- | --- |
| `COMPUTER_STALE_OBSERVATION` / `COMPUTER_SCREEN_CHANGED` | 未发出新输入，重新读屏后判断 |
| `COMPUTER_ACTION_UNCERTAIN` | 动作效果不确定，会话停止；核实实际界面，不换新键盲目重做 |
| `action_outcome=completed` 且 `observation_error` | 动作已有原生回执，只恢复读屏，不重复动作 |
| `native_is_error=true` | 原生工具拒绝/报错；不会被标成完整成功，检查本机提示和实际效果 |
| `COMPUTER_SESSION_NOT_FOUND` | 授权/项目不匹配，或会话已停止/重启失效；不复用旧观察 |
| `COMPUTER_TIMEOUT` | 本次原生请求无可确认结果；超时本身不能证明具体权限或兼容性原因 |

## 图片与隐私边界

截图作为标准 MCP `image` 内容块返回，结构化结果只保留元信息，不把 Base64 挤进文字 JSON。直接调用和 `operations_get/wait` 轮询都使用同一路径；原有 stdio 桥接保留图片块。

单图最多 3 MiB、一次最多 4 张、总内容最多 5 MiB；支持 PNG/JPEG，并验证编码和尺寸。超限返回明确错误，不偷偷降采样改变坐标。拒绝未知资源链接，原生输出不能诱导适配器自动读取任意文件或网址。

截图和应用正文在 Agent/Hub 的结果中设置 15 分钟逻辑有效期，过期查询会去掉它们；运行服务分批清理数据库中的过期内容。操作编号、元信息和审计回执保留。输入文字/元素值等不写入可读审计参数摘要；既有加密请求和持久回执机制仍生效。

这不承诺对 SQLite 空闲页、WAL、数据库备份做物理安全擦除，不会删除已经发给 ChatGPT 的图片，也不控制原生 Codex 的独立历史存储。桌面权限不是项目文件目录沙箱，应用内容可能超出项目目录。

## 实现与测试位置

核心：`agent/computer.py`、`shared/computer_contracts.py`、`shared/computer_media.py`；集成修改覆盖 Agent、Hub、MCP、OAuth、配置、CLI 和网页入口。原生通道：`agent/computer_appserver.py`，审批：`agent/computer_approvals.py` / `hub/computer_approvals.py`。联调面板：`web/computer.js` / `web/computer.css`。诊断：`scripts/check_computer_use.py`。

测试：`tests/test_computer.py`、`tests/test_computer_integration.py`、`tests/fake_computer_provider.py`。隔离端到端测试运行真实 Hub、加密 Agent 和真实 stdio 子进程，但该子进程生成合成画面，不控制用户电脑。测试记录、失败修正与最终全仓结果见 `docs/evidence/computer-use-20260913/` 和 `RELEASE.json`。

接口标准参考：MCP 2025-11-25 的 Tools/ImageContent，以及 OpenAI Codex Computer Use 的原生权限说明；本机实际 `tools/list` 才是本适配器动作兼容性的直接证据。

## 审批与故障恢复诊断

面板通过 SSE 得知审批变化，再通过登录态接口取得具体请求。连接正常时每 15 秒兜底刷新，断线时每 5 秒尝试，失败退避最多 30 秒，隐藏页面暂停轮询。相同请求保留按钮与焦点；过期或状态未确认时不能提交决定。

`operations_trace` 显示原生连接、读屏、输入前核对、输入、等待本人批准与审批结束阶段。数值字段 `native_call_ms`、`approval_wait_ms` 分离程序处理与人工等待；不存输入文本、原生回调正文或 stderr。

`computer_status` 可以恢复属于当前授权、项目及根目录的会话编号；其他授权只能看到设备正忙。失效原生连接会释放会话。`COMPUTER_SCREEN_CHANGED` 返回 `next=computer_observe`、`input_sent=false`，重新观察后才能决定新动作。`COMPUTER_APP_AMBIGUOUS` 要求选取准确应用路径。`COMPUTER_ACTION_UNCERTAIN` 仍禁止盲目重放。
