# Claude Code CLI 会话

CodePier 1.12.0 为 CLI 会话增加第三种原生程序 **Claude Code**，与 Pi、Codex 并列。该版本发布源码；是否已部署到现有 Hub / Agent 需单独核对运行版本。

## 使用

在「CLI 会话」选择项目，打开会话设置，将 CLI 切换到 **Claude**；也可以在项目入口直接点击 Claude。模型目录从所选节点的 Claude Code 初始化接口读取，不写死模型名称、服务商或思考档位。选择模型、工作目录以及可用的思考强度，发送第一条消息后才创建原生会话。

节点必须安装 `claude`，并由运行 Agent 的同一系统用户完成原生登录或配置。CodePier 沿用该用户的原生配置；面板不会收集 API Key、替用户登录，也不会自动安装 Claude Code。PATH 的显式配置保持优先。项目仍需允许写入及执行，节点本地 Shell 授权也必须开启。

Hub 和 Agent 均需要包含本功能。新版 Agent 广播 `native_chat_protocol: 3`，Hub 仍接受 Pi / Codex 使用的旧协议 1、2。新版面板请求旧 Agent 启动或读取 Claude 目录时，会明确提示更新 Agent，不创建永久等待的空会话。

## 会话能力

| 能力 | 行为 |
| --- | --- |
| 消息 | 原生流式文本、原生思考事件、工具调用和工具结果；完整消息替换对应增量，避免重复显示。 |
| 图片 | 已上传并校验的图片转换为 Claude 原生 base64 image block，文件名和 MIME 可回放，图片编码不回显到文本历史。 |
| 普通文件 | 使用已绑定附件的本机路径，由原生文件工具读取；不把文件路径冒充图片。 |
| 工具审批 | 只回答实际收到的权限请求，允许本次或拒绝；不增加永久权限规则，不注入跳过审批参数。 |
| 提问 | `AskUserQuestion` 支持单选、多选及其他答案；回答绑定原始问题、当前回合与请求指纹。 |
| 中断与跟进 | 可中断当前回合、排队跟进；中断回执等原生确认后完成，不把已入队当作成功。 |
| 模型 | 会话空闲时通过原生 `set_model` 切换，原生拒绝时保留原选择并显示错误。 |
| 思考强度 | 使用本机返回的档位，启动或恢复时通过 `--effort` 生效；运行中不提供未经验证的即时修改。 |
| 上下文整理 | 通过原生 `/compact`，完成与失败以原生 result 为准。 |
| 统计 | 展示原生 result 返回的 token、缓存 token 和费用；不估算缺失的上下文窗口比例。 |
| 历史 | 保留 CodePier 的私有回放和原生 UUID，停止后使用 `--resume UUID` 恢复同一项目、目录及 CLI 的原生对话。 |

Claude 当前不提供「立即补充」：界面禁用该选项并回到排队跟进。思考强度在运行期间禁用，提示停止后选择新强度再恢复。`/clear`、`/reset`、`/resume`、`/new` 不允许在一条 CodePier 记录内静默更换原生会话；请使用面板的新建或恢复操作。

## 原生协议

长驻子进程使用：

```text
claude --print --verbose
       --input-format stream-json --output-format stream-json
       --include-partial-messages --permission-prompt-tool stdio
       [--model MODEL] [--effort EFFORT] [--resume UUID]
```

stdin / stdout 由独立 worker 持有，不依赖网页保持打开。目录探测使用相同控制通道的 `initialize`，附加 `--no-session-persistence`，不发送用户消息或模型请求；探测退出或超时均回收进程。只提取模型、档位和指令白名单字段，原生 account、连接设置和凭据不进入目录响应。

实现对应 Anthropic 官方 Claude Agent SDK 的控制请求/响应格式；CodePier 不额外引入 SDK 运行依赖。初始化、设置切换、权限回答和中断均使用结构化消息，不通过拼接 Shell 指令控制 Claude。

## 权限与恢复边界

原生 Claude Code 仍以 Agent 系统用户权限运行，不是 CodePier 项目目录级操作系统沙箱。原有配置、工具审批和项目授权不会被本功能关闭。过期、重复、更改内容或过大的审批请求不会获得新授权；不支持的控制请求明确拒绝。

只有原生程序确认过的 UUID 才能恢复，且同一原生历史同一时刻只能由一个存活 worker 占有。原生 UUID 异常改变会终止当前桥接并保留原历史，而不是串到其他对话。操作系统杀死进程或机器重启后，进行中的请求不会自动重放；恢复历史不保证把未完成模型请求原地续上。

模型目录可读不等于账户已能成功调用该模型。登录失效、账户限制或模型请求错误，以原生程序返回的错误为准。恢复依赖节点上仍保留原生历史。删除网页记录不删除用户的 Claude 配置和原生历史。

## 验证入口

```bash
.venv/bin/python -m pytest -q tests/test_claude_cli.py tests/test_claude_browser.py
```

后端测试使用隔离 HOME 和确定性 Claude 协议模拟进程，覆盖真实 worker 管道、消息去重、审批、图片、设置、队列、中断、错误与恢复约束。浏览器测试通过实际 HTTP、Hub、Agent、worker 运行 Chromium / WebKit，并检查移动布局、历史、工具变更审阅及提问表单。它们不访问外部模型服务，不代替真实账户下的模型推理验证。

原生协议参考：Anthropic Claude Code CLI reference、Claude Agent SDK Python 的 `query.py` 和 `subprocess_cli.py`。版本差异以节点实际返回的目录与控制确认结果为准。
