# Web SSH 运维与验收手册

本文对应 2026-11-22 的 Web SSH 实现，协议版本为 `1`，用于部署准备、接入检查、故障定位和回归验收。文中的命令是操作入口；源码、构建产物、测试通过、发布包和线上部署应分别记录。本文不代表已升级任何 Hub、Agent 或 VPS。

Web SSH 由浏览器连接 Hub，Hub 通过独立的加密终端通道联系所选项目的 Agent，再由 Agent 使用 AsyncSSH 连接已保存的 VPS。VPS 不需要安装 CodePier Agent。SSH 账号在远端具备的权限就是终端的操作权限；项目授权控制谁能连接，不构成远端目录沙箱。[实现入口](../hub/ssh_terminal/api.py)、[Agent 终端通道](../agent/ssh_terminal_link.py)

## 1. 上线准备与开关

### 1.1 依赖和接入条件

| 位置 | 必要条件 |
| --- | --- |
| Hub | 包含 `hub/ssh_terminal/`、共享协议模块和匹配的面板生成物；沿用已登录面板身份、Space、CSRF 和项目执行授权。 |
| Agent | 包含 `agent/ssh_terminal.py` 与 `agent/ssh_terminal_*.py`，按 `requirements-agent.txt` 的哈希锁安装依赖；本次锁定 `asyncssh==2.24.1`。主 Agent 通道与独立终端通道均须在线。 |
| 项目 | 启用、可写、允许执行任务；当前用户拥有项目写入和执行权限，Agent 本机 Shell 策略也允许该项目。 |
| VPS 记录 | 已启用，已分配给该项目；保存的地址、端口、账号和密码有效。连接由该项目的 Agent 发起，网络可达性以 Agent 所在电脑为准。 |
| VPS 系统 | SSH 服务允许 PTY。自动模式会探测远端 `tmux`；版本至少为 `2.6` 时使用持久会话，明确缺失或版本不支持时使用普通会话。 |
| 浏览器 | 从同源 HTTPS 面板访问，支持 WebSocket、现代 JavaScript 和 xterm；localhost/回环地址允许 HTTP，供隔离测试使用。 |

Web SSH 的认证实现支持保存的密码及 keyboard-interactive；没有新增浏览器私钥上传、SSH Agent 转发、ProxyJump 配置或 SFTP 传输。Agent 的 AsyncSSH 连接显式关闭私钥发现、SSH Agent、X11 和 Agent 转发，不读取用户私钥。传统 VPS「检查连接」仍使用原有 OpenSSH/sshpass 链路，两条链路分别验收。[认证实现](../agent/ssh_terminal_transport.py)、[项目授权](../agent/ssh_terminal_manager.py)、[tmux 探测](../agent/ssh_terminal_tmux.py)

### 1.2 功能开关的准确含义

Hub 读取 `CODEPIER_SSH_TERMINAL_ENABLED`，默认 `1`。使用 `1` 启用新建终端，`0` 暂停新建；开关关闭时，能力接口返回 `enabled: false` 和 `FEATURE_DISABLED`，创建接口拒绝新会话。

这个开关不批量结束已有会话，也不撤销已有连接的执行权限。需要结束某个会话时，在终端执行「结束会话」并核对结果；需要收回某个身份或项目的访问权时，使用已有权限、VPS 分配或设备启停管理。权限变化会重新校验并使旧连接失去输入能力，远端已有进程是否停止仍需单独确认。[开关与授权实现](../hub/ssh_terminal/service.py)

数值配置由 Hub 和 Agent 启动时读取进程环境。更改服务配置后，通过该安装方式已有的维护流程使环境生效。仅在交互 Shell 中设置变量不会改变已运行的服务。

`compose.yml` 已透传功能开关，`.env.example` 提供 `CODEPIER_SSH_TERMINAL_ENABLED=1`。Compose 安装可在实际使用的 `.env` 中设置此项为 `0` 暂停新建，并通过原有维护流程使环境生效。其余数值变量未逐项透传；仅向 `.env` 添加数值变量不会自动注入容器，需在经过审阅的覆盖文件中显式配置，例如：

```yaml
# compose.web-ssh.yml 示例；合并到现有 Hub 服务配置。
services:
  hub:
    environment:
      CODEPIER_SSH_TERMINAL_MAX_SESSIONS_PER_USER: "8"
      CODEPIER_SSH_TERMINAL_MAX_SESSIONS_PER_HUB: "64"
```

以下命令只检查合并配置，不执行部署：

```bash
docker compose -f compose.yml -f compose.web-ssh.yml config --quiet
```

Agent 的数值配置应放在实际启动 Agent 的服务环境中。`CODEPIER_SSH_TERMINAL_ENABLED` 是 Hub 的新建入口开关，Agent 不读取这个布尔开关。[Compose 配置](../compose.yml)、[环境变量样例](../.env.example)、[共享配置](../shared/ssh_terminal_config.py)

### 1.3 建议升级顺序

1. 在隔离环境按锁文件安装依赖，完成本手册的协议、Agent、Hub、真实 TCP 和前端生成物检查，建立本轮证据。
2. 按现有更新流程备份 Hub 一致性数据库、匹配的主密钥文件与状态目录，保留 Agent 配置和状态。记录实际运行版本及可恢复路径。`HUB_STATE_DIR` 分离布局必须按实际状态位置备份。
3. 先准备匹配的 Hub 服务端与静态资源；首次开放前可以将新建开关设为 `0`。`app.js`、终端 bundle、`index.html` 与资产哈希清单一起更新。
4. 通过现有 Agent 管理流程更新连接电脑，确认新运行时实际包含 AsyncSSH，并建立独立终端通道。旧 Agent 或缺少依赖的 Agent 会显示升级提示。
5. 选择获准的测试 VPS，核对主机信任、PTY、普通模式与 tmux 模式、断线恢复、尾部输出、关闭与撤权，再按部署计划开放新建入口。

更新会影响正在使用的连接。普通 SSH 会话依赖原 Agent/SSH 连接；tmux 会话的恢复仍要求远端会话存在、凭据有效和权限重新通过。先完成当前运维操作，再安排当前控制通道的升级。实际部署与回滚遵循 [面板更新](PANEL_UPDATE.md)、[Agent 生命周期](AGENT_LIFECYCLE.md)、[发布清单](RELEASING.md) 和 [项目升级技能](../skills/hub-agent-upgrade/SKILL.md)。

## 2. 面板操作与会话行为

在 **VPS → 打开终端** 连接。只有一个可用项目时直接进入；多个项目时在卡片内选择连接项目，当前登录与 Space 内记住选择。已打开的同一 VPS/项目优先返回原标签；「新建」和「分屏」会创建独立会话。路由为 `#vps?view=terminal&session=<会话编号>`，链接本身不包含凭据，也不能替代登录与授权。

工作区支持标签切换、重命名、搜索、复制、粘贴、全屏、字号和显示主题。桌面可并排使用两个独立终端；窄屏以标签切换并提供 Esc、Tab、方向键、Ctrl、Alt、Ctrl-C 等辅助键。普通 Ctrl-C 发送中断字符，复制使用系统平台对应的终端复制快捷键或「复制」按钮。

多行、控制字符或较长的粘贴会显示可编辑预览；默认单次上限 256 KiB。提交后分块发送并等待输入缓冲，支持停止；停止只影响尚未发送的部分，不撤回已发送的输入，也不额外添加回车。终端输入、输出、验证码和附着票据不存入 localStorage、sessionStorage 或 IndexedDB。[工作区](../web/vps-terminal/layout.mjs)、[视图](../web/vps-terminal/terminal-view.mjs)、[粘贴](../web/vps-terminal/paste.mjs)

| 操作或事件 | 实际行为 |
| --- | --- |
| 在面板切换到其他页面 | 隐藏并保留原 xterm DOM 与本地连接；返回时不重新创建 shell。 |
| 关闭终端标签 | 分离本地浏览器附着，不等同于结束远端会话。最后一个标签关闭后 URL 回到终端会话页，后台刷新不会重开旧标签。 |
| 关闭网页或网络暂断 | 当前附着消失；自动重连使用原 session ID 和新的短期票据。 |
| 刷新整个网页 | 新浏览器渲染器重新申请附着；使用 tmux 重绘或满足条件的普通会话历史恢复。 |
| 「结束会话」 | 发出显式关闭请求。收到 `close_unknown` 时继续检查原会话状态，不能把请求已提交视为远端已结束。 |
| 退出登录、切换 Space、Space 权限被撤销 | 清除本地终端、票据、认证表单和内存选择；服务端重新校验身份与执行权。 |
| 在另一窗口打开相同会话 | 单写入者冲突会阻止输入；需要用户选择「接管」，并以服务端最新 writer epoch 核对控制权。 |

浏览器的自动重连采用指数退避，基准间隔由 0.5 秒增至 8 秒并加入抖动，最多 8 次。达到次数后显示人工重试入口。权限撤销、主机密钥/认证问题、协议不兼容、原会话不存在等不会持续自动重连。[会话状态与路由](../web/vps-terminal/store.mjs)、[传输与重试](../web/vps-terminal/transport.mjs)

### 输入确认与尾部输出

**未确认输入从不自动重发。** 断网时服务器可能已经接收输入，也可能尚未接收；界面显示「有输入未确认，未自动重发」。先观察远端状态再决定下一步，不因画面中断重复提交结果不明的写命令。`input_ack` 只描述输入接收/转发状态，不代表远端命令执行成功。

输出按字节偏移恢复，只确认 xterm 的 `write` callback 已消费的字节。浏览器 ACK 每 16 ms 或累计 64 KiB 合并，初始附着、恢复边界和关闭前立即提交已消费偏移。恢复画面完成前不开放键盘；历史回放阶段抑制自动终端应答。HTTP 查询或关闭响应中的 `exited` 元数据不提前关闭仍在输出的浏览器附着，最终画面依赖有序终端流的结束事件。`output_complete: false` 必须保留为输出不完整，不能展示成完整成功。[传输实现](../web/vps-terminal/transport.mjs)、[HTTP 状态合并](../web/vps-terminal/store.mjs)、[输出流控](../shared/ssh_terminal_flow.py)

## 3. 普通 SSH 与 tmux 持久会话

界面创建请求使用 `mode: auto`。Agent 只在探测明确确认 tmux 缺失或版本不足时退回普通模式；tmux 探测失败、创建结果不明或远端错误不会悄悄创建另一个普通 shell。

| 情况 | 普通模式 `direct` | 持久模式 `tmux` |
| --- | --- | --- |
| 浏览器短暂断线，Agent 与 SSH 仍存活 | 可恢复原 PTY；需要所需字节仍在有界环形缓冲中。 | 可恢复未消费输出，必要时重新附着并重绘。 |
| 新页面/新渲染器 | 仅当完整所需历史仍可用时回放；发现缺口显示 `HISTORY_GAP`。 | 附着同一远端 tmux 会话，使用新 PTY 重绘当前屏幕。 |
| 浏览器分离超时 | 授权仍有效时默认保留 600 秒，超过期限关闭普通连接。 | 默认 30 秒后关闭 tmux 附着 PTY，远端会话继续存在。 |
| SSH 连接丢失或 Agent 重启 | 原 shell 无法承诺恢复，不自动创建新 shell。 | 重新授权和认证后只查找、附着原远端会话。 |
| 原远端会话已消失 | 显示已结束/不可恢复，由用户新建。 | 返回 `REMOTE_SESSION_MISSING`，恢复路径不使用 `new-session -A`。 |
| 权限撤销/授权租约过期 | 停止输入并关闭 SSH 连接。 | 停止输入并断开附着；远端任务可能继续运行。重新进入仍须重新授权。 |

以上保留时间以连接仍合法、相应进程仍存活为条件；Hub 不可达时的授权租约也会独立到期。默认租约 30 秒、续租间隔 10 秒，不会因网页仍显示旧画面而继续允许输入。

每个持久会话使用独立的 tmux socket `codepier_<session_uuid>` 和会话名 `cp_<session_uuid>`，并使用 `-f /dev/null` 避免继承远端 tmux 用户配置。显式结束只针对该生成的会话，不调用共享 `kill-server`。默认 tmux history-limit 为 10,000 行。远端 shell/进程退出、tmux 被移除或远端主机重启后，不能通过浏览器重新创建来冒充原会话恢复。[tmux 控制器](../agent/ssh_terminal_tmux.py)、[生命周期与租约](../agent/ssh_terminal_manager.py)

## 4. 密码、验证码与主机信任

### 4.1 密码与 keyboard-interactive

保存的密码仍由 Hub 主密钥加密保管，浏览器读取 VPS 元数据时不收到密码。新建、恢复或关闭已分离的 tmux 会话需要认证时，Hub 在当前授权通过后才临时解密并送到专用加密终端通道。Agent 连接结束后清理临时引用。

服务器要求额外认证时，终端区域出现紧凑表单。`echo: false` 使用密码输入框，回答以 `auth_answer` 控制消息发送，不进入普通键盘输入通道。提交、取消、超时或销毁视图时清空表单。挑战最多 8 个问题，认证轮次有界；认证总超时与挑战时限都生效，不能依靠反复显示验证码无限延长连接。[Hub 认证转发](../hub/ssh_terminal/service.py)、[AsyncSSH 认证](../agent/ssh_terminal_transport.py)

如果常用认证需要较长人工响应，可在经过验收的 Hub/Agent 服务配置中统一调整 `CODEPIER_SSH_TERMINAL_LOGIN_TIMEOUT`，并重新验证超时、取消、断网和租约边界。`AUTH_TIMEOUT_SECONDS` 是终端 WebSocket 首帧/握手时限，不是 SSH 验证码输入时限。

### 4.2 known_hosts 的实际位置

默认信任文件位于 **运行 Agent 的服务账号**，不是浏览器或 Hub 的用户目录：

- 用户目录：`~/.ssh/known_hosts`、`~/.ssh/known_hosts2`。
- Linux/macOS 系统目录：`/etc/ssh/ssh_known_hosts`、`/etc/ssh/ssh_known_hosts2`。
- Windows 系统目录：`%PROGRAMDATA%/ssh/ssh_known_hosts`、`%PROGRAMDATA%/ssh/ssh_known_hosts2`。

`strict` 要求匹配已有可信记录；`accept-new` 只接受首次未知主机，在 SSH 证明主机密钥持有权后、发送密码前追加记录。已有记录不匹配或标记撤销时仍拒绝连接。非 22 端口按 `[host]:port` 精确匹配，不回退使用 22 端口记录。首次信任追加使用文件锁和完整行追加，避免并发首次连接互相覆盖。[主机信任实现](../agent/ssh_terminal_trust.py)

在 Agent 所在电脑、相同服务账号与运行环境下进行只读检查，例如：

```bash
python -c "from pathlib import Path; print(Path.home())"
ssh-keygen -F '[203.0.113.10]:2222' -f ~/.ssh/known_hosts
ssh-keygen -lf ~/.ssh/known_hosts
```

将示例地址和端口替换为保存的连接。未知或变化的指纹应通过 VPS 控制台或已建立的可信渠道核对后再维护记录。不要通过清空 known_hosts、删除整个信任库或关闭校验消除错误。

当前实现会保守检查 Agent 的 `.ssh/config`、系统 `ssh_config` 及其 Include。发现 `UserKnownHostsFile`、`GlobalKnownHostsFile`、`HostKeyAlias`、`KnownHostsCommand`、`VerifyHostKeyDNS`、`RevokedHostKeys` 或 `CheckHostIP` 等自定义信任指令时返回 `HOST_TRUST_CONFIGURATION`，避免使用不同于原生 SSH 的信任政策。当前没有面板/环境变量形式的自定义信任来源配置；不要把内部测试用的 `trust_sources` 参数当作已交付的运维配置入口。应先审阅现有信任策略与接入方式，再安排受控适配。

## 5. HTTPS、反向代理和网络

### 5.1 需要连通的路径

| 路径 | 用途 |
| --- | --- |
| `/api/vps-terminal/capabilities` | 已登录用户查询所选 VPS/项目的可用能力、限制及升级原因。 |
| `/api/vps-terminal/sessions` 及子路径 | 创建、查询、获取附着票据、分离、接管和关闭；写操作使用面板 CSRF。 |
| `/api/vps-terminal/sessions/{id}/diagnostics` | 当前会话拥有者在原 Space 与项目执行授权下读取诊断快照。 |
| `/ws/vps-terminal` | 浏览器到 Hub 的 WebSocket，每个会话一个连接。 |
| `/agent/vps-terminal/{device_id}` | Agent 到 Hub 的独立加密 WebSocket，依赖该设备现有主连接的有效授权。 |

浏览器 WS 的 Origin 必须与 Hub 实际识别的请求 scheme、host、port 一致，拒绝 `Origin: null`、跨站请求和非回环地址的明文 HTTP。Cookie 登录态与一次性附着票据共同校验，票据放在首个认证消息中，不放在 URL。代理应保留真实 Host 与 Origin，并正确传递 HTTPS scheme。[浏览器 Origin 校验](../hub/ssh_terminal/browser.py)、[HTTP/WS 适配](../hub/ssh_terminal/api.py)

Hub 主入口启用 `proxy_headers=True`，只信任 `FORWARDED_ALLOW_IPS` 指定的代理来源，默认 `127.0.0.1`。TLS 在反代终止时，把实际受信代理地址加入该设置；不要为消除 Origin 错误直接信任任意来源。`HUB_PUBLIC_URL` 应与正式面板外部地址一致，但它不替代本次 WebSocket 的 Origin 校验。[Hub 启动入口](../hub/__main__.py)

生产使用项目的 `python -m hub run` 启动入口；自行托管 ASGI 时必须显式接入 `TerminalAwareWebSocketProtocol`。该适配器在应用读取完整消息之前，分别约束浏览器与 Agent 终端路径的消息、分片及待处理消息数量，并关闭终端压缩。直接使用普通 `uvicorn` 默认协议启动应用会绕过这些传输边界；仅设置 `workers=1` 无法补回。[终端协议适配器](../hub/ssh_terminal/server_protocol.py)

### 5.2 Nginx 参考片段

下面片段用于已有 TLS 站点；按实际 Hub upstream 和代理地址调整，不覆盖现有证书、其他路由或访问策略。`map` 放在 Nginx `http` 上下文，`location` 放在该站点的 `server` 上下文。

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

location ~ ^/(ws/vps-terminal$|agent/vps-terminal/) {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $http_host;
    proxy_set_header Origin $http_origin;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 75s;
    proxy_send_timeout 75s;
}
```

`/api/` 继续使用原有同源 HTTP 代理，并保持 Cookie、CSRF、Space 请求头正常传递。浏览器应用心跳默认 15 秒、无响应判断为 45 秒；Hub/Agent 接收等待使用心跳倍数，Agent 的 WebSocket 还配置协议层 ping/pong。代理空闲超时应高于所用心跳窗口；上面 75 秒适用于默认配置，调整心跳后也应调整代理。SSH keepalive 是 Agent 到 VPS 的另一层检测，默认 15 秒、3 次。[通道心跳](../agent/ssh_terminal_link.py)、[共享配置](../shared/ssh_terminal_config.py)

保持现有 Hub `workers=1` 部署方式。活动会话、短期票据和 Agent 通道归当前 Hub 进程所有；本实现没有跨 worker/跨副本的会话 owner 路由。不能把同一个终端的 HTTP 与 WS 请求随机分配到多个 Hub 实例来获得透明高可用。[进程入口](../hub/__main__.py)、[会话所有权](../hub/ssh_terminal/service.py)

## 6. 容量、超时和配置核对

数值环境变量名称为 `CODEPIER_SSH_TERMINAL_` 加下表的大写后缀。字节值使用十进制整数，不能写 `256KiB`。除时间字段允许小数外，其余字段使用整数。非法类型、越界值和不满足关联约束的组合会在初始化时失败。[配置唯一来源](../shared/ssh_terminal_config.py)

### 6.1 容量与画面

| 环境变量后缀 | 默认值 | 含义 |
| --- | ---: | --- |
| `MAX_SESSIONS_PER_USER` | 8 | 当前用户/Space 的会话上限，Hub 和 Agent 均有校验。 |
| `MAX_SESSIONS_PER_AGENT` | 16 | 每个连接电脑的会话上限。 |
| `MAX_SESSIONS_PER_HUB` | 64 | Hub 总会话数量上限。 |
| `AGENT_BUFFER_BYTES` | 134217728 | Agent 总缓冲预算，128 MiB。 |
| `HUB_BUFFER_BYTES` | 67108864 | Hub 总缓冲预算，64 MiB。 |
| `RING_BYTES` | 2097152 | 每个 Agent 会话的输出环形缓冲，2 MiB。 |
| `OUTPUT_HIGH_WATER_BYTES` | 262144 | 未消费输出信用高水位，256 KiB。 |
| `OUTPUT_LOW_WATER_BYTES` | 65536 | 输出恢复低水位，64 KiB。 |
| `HUB_RELAY_BYTES` | 524288 | Hub 中继队列基准预算，512 KiB。 |
| `SSH_WINDOW_BYTES` | 262144 | SSH 通道接收窗口，256 KiB。 |
| `FRAME_BYTES` | 16384 | 单帧输出 payload 上限，16 KiB。 |
| `INPUT_FRAME_BYTES` | 16384 | 单帧输入 payload 上限，16 KiB。 |
| `CONTROL_BYTES` | 8192 | 控制消息大小上限，8 KiB。 |
| `PENDING_INPUT_BYTES` | 65536 | 待确认/待转发输入预算，64 KiB。 |
| `PASTE_BYTES` | 262144 | 单次粘贴大小上限，256 KiB。 |
| `INPUT_RECEIPTS` | 256 | 有界输入接收回执窗口。 |
| `SCROLLBACK_LINES` | 10000 | 浏览器 xterm 本地滚动历史行数。 |
| `TMUX_HISTORY_LINES` | 10000 | 每个专用远端 tmux server 的 history-limit。 |
| `MIN_COLS` / `MAX_COLS` | 2 / 1000 | PTY 列数范围。 |
| `MIN_ROWS` / `MAX_ROWS` | 1 / 500 | PTY 行数范围。 |
| `RESIZE_COALESCE_MS` | 60 | 浏览器尺寸变更合并窗口，毫秒。 |

数量和字节预算同时约束准入，因此「Hub 上限 64」不保证任意分布下均能建立 64 个会话。Hub 还计入活动设备通道队列，Agent 计入环形缓冲、SSH 窗口和输入预算；已分离的持久会话、结果不明的远端会话仍可能占用额度。关闭本地标签不等同于释放远端会话。预算控制终端缓冲，不是整个 Python 进程、浏览器或远端 tmux 的 RSS 上限。[Hub 准入](../hub/ssh_terminal/service.py)、[Agent 准入](../agent/ssh_terminal_manager.py)

Hub 对正在握手和已连接的浏览器终端 WS 也设上限，保留有限的重连重叠空间。`TERMINAL_CONNECTION_LIMIT` 与 `SESSION_LIMIT` 分别表示连接槽位和会话/内存预算不足，不应通过重复打开更多标签解决。

### 6.2 时间参数

| 环境变量后缀 | 默认值（秒） | 含义 |
| --- | ---: | --- |
| `HEARTBEAT_SECONDS` | 15 | 终端应用心跳间隔。 |
| `SSH_KEEPALIVE_INTERVAL` | 15 | Agent 到 VPS 的 SSH keepalive 间隔。 |
| `SSH_KEEPALIVE_COUNT` | 3 | SSH keepalive 失败计数；此项是次数。 |
| `TICKET_TTL_SECONDS` | 30 | 一次性浏览器附着票据有效期。 |
| `AUTH_TIMEOUT_SECONDS` | 5 | WebSocket 首帧/专用通道握手时限。 |
| `LEASE_SECONDS` | 30 | 执行授权租约。 |
| `LEASE_RENEW_SECONDS` | 10 | Hub 续租间隔，必须短于租约。 |
| `DIRECT_DETACH_SECONDS` | 600 | 普通会话分离保留时间。 |
| `ATTACHMENT_GRACE_SECONDS` | 30 | tmux 附着保留及输出暂停宽限。 |
| `CONNECT_TIMEOUT` | 10 | SSH 建连时限。 |
| `LOGIN_TIMEOUT` | 15 | SSH 登录/挑战时限。 |
| `WRITE_TIMEOUT` | 5 | 有界通道写入/控制操作等待。 |
| `SHUTDOWN_TIMEOUT` | 5 | 清理关闭的基准时限。 |

协议帧、缓冲和心跳参数需要在 Hub 与 Agent 间协调配置，不能单独调高一端就假定另一端同步支持。浏览器从能力接口取得公开限制；其 `output_pending_bytes`、`heartbeat_timeout_seconds`、`max_user_sessions` 是派生字段，`reconnect_max_ms` 由实现固定，不是另外可用的同名环境变量。

主要关联约束为：低水位小于高水位；帧能放入信用窗口；环形缓冲至少覆盖高水位和 SSH 接收窗口；Hub 中继至少覆盖高水位加一帧；输入与粘贴预算至少容纳一帧；握手期限不超过票据 TTL。全部数值范围与约束以 `TerminalConfig` 为准。

在对应服务的代码和环境中打印实际数值配置，只输出非秘密的终端限制：

```bash
python - <<'PY'
import json
import os
from dataclasses import asdict
from shared.ssh_terminal_config import TerminalConfig

enabled = os.getenv("CODEPIER_SSH_TERMINAL_ENABLED", "1").lower() in {"1", "true", "on", "yes"}
print("Hub new-session switch:", enabled)
print(json.dumps(asdict(TerminalConfig.from_env()), indent=2))
PY
```

这条命令必须使用与服务一致的环境；在另一个交互 Shell 中运行，只能证明该 Shell 的配置。

## 7. 常见错误与处理

| 原因码/现象 | 优先核查和操作 |
| --- | --- |
| `FEATURE_DISABLED` | Hub 新建入口开关；已有会话的存在不说明新建已开放。 |
| `LOGIN_REQUIRED` / `AUTH_REQUIRED` / `SESSION_EXPIRED` | 当前面板登录已失效；自动重试停止，重新登录后再进入终端。 |
| `AGENT_OFFLINE` | 所选项目所在电脑和主 Agent 通道。 |
| `AGENT_UPGRADE_REQUIRED` / `SSH_DEPENDENCY_MISSING` | Agent 运行版本、AsyncSSH 锁定依赖、独立终端 WS 路由；主 Agent 在线不代表该专用通道已建立。 |
| `SSH_TERMINAL_FORBIDDEN` / `AUTH_REVOKED` | 当前登录、Space 成员资格、项目写入/执行权、Agent Shell 策略、VPS 分配与设备启停。 |
| `TARGET_REVISION_CHANGED` / `PROJECT_MAPPING_CHANGED` | 保存的连接或项目映射变化。旧会话目标不会被悄悄改写，重新核对后新建。 |
| `VPS_CREDENTIAL_UNAVAILABLE` / `SSH_AUTH_FAILED` | 保存的凭据、Hub 配套主密钥、账号登录策略；不要把密码放入诊断日志或 URL。 |
| `HOST_KEY_UNKNOWN` / `HOST_KEY_CHANGED` / `HOST_KEY_REVOKED` | Agent 服务账号的实际信任文件、地址与端口、可信渠道确认的指纹。 |
| `HOST_TRUST_CONFIGURATION` / `HOST_TRUST_UNAVAILABLE` | 自定义原生 SSH 信任设置、文件读取权限、文件格式或不支持的公钥记录。 |
| `HOST_TRUST_BUSY` | 首次信任文件正被更新；原记录核验完毕后重试，不删除锁对应的信任库。 |
| `SSH_CONNECT_TIMEOUT` / `SSH_CONNECTION_FAILED` | 从 Agent 到保存的 VPS 地址与端口的网络、SSH 服务、登录时限和账号政策。 |
| `AUTH_CHALLENGE_EXPIRED` / `SSH_AUTH_CHALLENGE_REQUIRED` | 挑战是否超时、是否在当前页面作答、当前连接是否需要额外认证。 |
| `PERSISTENCE_UNAVAILABLE` / `PERSISTENCE_PROBE_FAILED` | VPS 的 tmux 版本、探测命令结果与账号可执行范围；失败时不会自动创建另一个 shell。 |
| `WRITER_CONFLICT` / `STALE_WRITER` | 另一窗口持有控制权；确认需要后使用「接管」。 |
| `HISTORY_GAP` | 普通会话所需历史已超出保留范围或输出未完整到达。先确认远端状态，再决定是否新建。 |
| `REMOTE_SESSION_MISSING` / `SESSION_NOT_RECOVERABLE` | 原 shell/tmux 已不存在或普通 SSH 已断开；恢复不等同于新建。 |
| `INPUT_UNCERTAIN` | 部分输入是否到达未确认；系统未重发。查看远端实际状态后操作。 |
| `INPUT_BACKPRESSURE` / `OUTPUT_BACKPRESSURE` | 输入或输出达到有界容量。先等待接收/恢复，观察网络与消费进度；不要只扩大所有缓冲。 |
| `SESSION_LIMIT` / `TERMINAL_CONNECTION_LIMIT` | 会话、连接槽位或总缓冲预算。核对原会话并显式结束不再需要的会话。 |
| `ORIGIN_REJECTED` / WS 403 | 面板外部 Origin、保留的 Host、HTTPS 转发和 `FORWARDED_ALLOW_IPS`。 |
| `ATTACH_TICKET_INVALID` / WS 4401 | 登录态变化、票据过期/复用、窗口身份不匹配；通过当前页面重新申请票据。 |
| `PROTOCOL_MISMATCH` / `INVALID_FRAME` / `INVALID_ACK` | Hub、Agent、静态 bundle 是否来自同一套已核验构建，是否存在代理混发旧资产或非法帧。 |
| `CLOSE_UNKNOWN` | 远端结束结果尚未确认；使用「检查状态」核对原 session，不通过删除记录伪造完成。 |
| `DIAGNOSTIC_BUSY` | 本会话诊断正在进行或距上次查询不足一秒；稍后点击「刷新」，不会为诊断创建新 SSH 连接。 |

Web SSH 的会话编号不是普通任务的 `operation_id`。终端流不经过普通执行日志/任务账本，不用重新提交 `exec` 来恢复已建立的交互终端。[Hub 错误与会话管理](../hub/ssh_terminal/service.py)、[协议校验](../shared/ssh_terminal_protocol.py)

## 8. 诊断步骤与数据边界

在当前终端工具栏点击 **诊断**，直接展开可收起的会话面板。三段状态分别为浏览器 ↔ Hub、Hub ↔ 连接电脑、连接电脑 ↔ VPS；Agent 无法及时确认 SSH 时显示「未确认」，不把网络中断解释成远端 shell 已结束。

面板展示已取得的输入/输出字节、本次控制查询往返，以及可展开的连接、PTY、Hub 准备和附着耗时、会话事件计数、缓冲值与版本。**控制往返**是这次 Hub 到 Agent `status` RPC 的实际耗时，包含处理时间，不是纯网络延迟。缺失值显示「未采集」或不列出，不用 0 冒充测量结果；数据是手动采集的状态快照，不自动轮询，也不执行新的 VPS 命令。关闭面板、销毁终端或切换身份后，迟到的响应不会重建旧视图。[诊断接口与采样](../hub/ssh_terminal/service.py)、[安全字段投影](../hub/ssh_terminal/diagnostics.py)、[诊断面板](../web/vps-terminal/diagnostics.mjs)

1. 在 VPS 卡片确认选中的项目、电脑、启停状态。记录当前会话编号、发生时间、原因码和重现步骤，先确认是否是原会话恢复。
2. 检查 Hub `/healthz`、电脑页实际 Agent 版本与在线状态；已安装的本机管理入口可用 `codepier-agent status`，Windows 为 `codepier-agent.ps1 status`。
3. 在 Agent 实际运行时核对依赖：

```bash
python -c "from importlib.metadata import version; print(version('asyncssh'))"
python -c "from agent.ssh_terminal_transport import available; print(available())"
python -m pip check
```

4. 在已登录面板查看 `/api/vps-terminal/capabilities?vps_id=...&project_id=...` 的 `available`、`reason_code`、`protocol_version`，再查看原 session 的 `state`、`mode`、`recoverability`、`output_complete`。这些接口复用当前面板身份；不把管理 Cookie 或 CSRF 复制到共享脚本。
5. 浏览器 Network 检查 `/ws/vps-terminal` 是否升级、是否收到认证/附着控制和持续心跳；Hub 代理检查 `/agent/vps-terminal/...` 是否被转发。WS 帧可能含终端输入、输出与验证码，向外提供诊断时只保留必要的状态与偏移信息。
6. 对主机信任错误按第 4 节检查 Agent 服务账号的文件；对断线恢复错误按第 3 节核实原远端会话是否还存在。对结束结果不明，继续查询原会话。

Hub 的 `vps_terminal_sessions` 仅存身份、目标引用、生命周期和允许的诊断元数据。审计记录 `vps_terminal.create`、`connect`、`disconnect`、`recover`、`takeover`、`close` 和 `revoke` 等生命周期动作，不记录键盘输入或终端画面。指标使用固定名称和有界耗时桶，当前会话诊断不会公开其他用户会话的指标；未能归因到本会话的共享队列不作为本会话测量展示。当前没有终端全文录制、全文审计回放或专用 Prometheus 指标面板。远端 shell 自己的历史行为由 VPS 的系统配置决定。[持久化字段](../hub/ssh_terminal/repository.py)、[审计写入](../hub/ssh_terminal/service.py)、[指标收集](../hub/ssh_terminal/diagnostics.py)

经授权的 Hub 运维人员需要检查数据库时，使用真实状态目录的 `hub.sqlite3`，以只读方式查询，例如：

```bash
sqlite3 -readonly /path/to/actual-hub-state/hub.sqlite3 \
  "SELECT id, mode, state, reason_code, datetime(updated, 'unixepoch') AS updated_utc FROM vps_terminal_sessions ORDER BY updated DESC LIMIT 20;"
```

实际状态目录可能由 `HUB_STATE_DIR` 与稳定文件目录分离，按 [状态布局实现](../hub/state_layout.py) 核对。不要直接修改 writer/stream epoch、目标修订号、状态字段或删除运行中的会话来解除限制；这会破坏恢复与控制权校验。诊断截图、数据库摘录和日志按项目既有私有证据规范保存。

## 9. 回归命令与验收边界

遵循 [开发与验证](DEVELOPMENT.md)、[验证工具索引](VERIFICATION_TOOLS.md) 和 [review-and-verify](../skills/review-and-verify/SKILL.md)。使用 Python 3.13、前端构建 Node.js 24，以及本次代码对应的哈希锁。新环境示例：

```bash
python3.13 -m venv .venv-web-ssh
.venv-web-ssh/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv-web-ssh/bin/python scripts/check_dependencies.py
npm --prefix web/mcp-apps ci --ignore-scripts
```

### 9.1 构建与不依赖浏览器的验证

```bash
npm --prefix web/mcp-apps run format:check
npm --prefix web/mcp-apps run build
node web/mcp-apps/build-terminal.mjs --check
.venv-web-ssh/bin/python scripts/build_web_assets.py
.venv-web-ssh/bin/python scripts/build_web_assets.py --check
node --test tests/vps_terminal_frontend.test.mjs tests/test_lazy_assets.mjs
.venv-web-ssh/bin/python -m ruff check agent hub shared scripts tests
.venv-web-ssh/bin/python -m mypy
```

`mypy` 按项目既有的增量范围执行，不能据此宣称全仓或全部终端代码都已类型检查。终端生成物包括 `web/vps-terminal.js`、CSS、依赖/哈希 manifest 和第三方许可证；修改源文件后再生成整站 `web/index.html` 与 `web/assets-manifest.json`，保持版本一致。

相关 Python 模块：

```bash
webssh_tests_dir="$(mktemp -d /tmp/codepier-webssh-tests.XXXXXX)"
.venv-web-ssh/bin/python -m pytest -q --basetemp "$webssh_tests_dir/python" \
  tests/test_ssh_terminal_protocol.py \
  tests/test_ssh_terminal_flow.py \
  tests/test_ssh_terminal_channel.py \
  tests/test_ssh_terminal_agent.py \
  tests/test_ssh_terminal_hub.py \
  tests/test_ssh_terminal_api.py \
  tests/test_ssh_terminal_end_to_end.py
```

`--basetemp` 使用本轮新建的专属目录，避免合成项目落入 Agent 私有保护目录。pytest 会管理和清理指定目录，不要传入已有工作目录或共享目录；Windows 也应显式选择本轮专用的测试目录。CodePier 的 `project_clean_v1` 运行配置拥有 `TMPDIR`、`TMP` 和 `TEMP`，通过 `operation.env` 覆盖会返回 `RUNTIME_ENV_RESERVED`；保留运行配置，使用测试工具的目录参数，不以 Shell 赋值绕过限制。真实 TCP 集成测试启动隔离 Hub、实际 Agent 和本地 AsyncSSH peer，验证 PTY、二进制输出、流控、恢复、输入与最终输出，不连接用户 VPS，不代表生产验收。[运行环境约束](../agent/project_runtime.py)、[TCP 测试](../tests/test_ssh_terminal_end_to_end.py)

### 9.2 浏览器与 tmux

```bash
.venv-web-ssh/bin/python -m playwright install chromium webkit
webssh_browser_dir="$(mktemp -d /tmp/codepier-webssh-browser.XXXXXX)"
.venv-web-ssh/bin/python -m pytest -q --basetemp "$webssh_browser_dir/panel" tests/test_vps_terminal_panel_browser.py
.venv-web-ssh/bin/python -m pytest -q --basetemp "$webssh_browser_dir/vps" tests/test_vps_ui.py
.venv-web-ssh/bin/python -m pytest -q --basetemp "$webssh_browser_dir/tmux" \
  tests/test_ssh_terminal_agent.py -k tmux_create_recover_close_never_creates_during_recovery
```

面板终端测试对 Chromium 和 WebKit 分别运行，使用真实 app 路由和 xterm、浏览器本地 HTTP/WS 夹具，覆盖懒加载、桌面/手机布局、底部辅助键实际命中、分屏重挂载、DOM 保留、关闭标签、Space 清理、诊断快照、项目选择与升级提示。现有 VPS UI 测试覆盖 Chromium/WebKit 基础管理回归。独立 `tests/vps_terminal_browser_smoke.mjs` 另覆盖真实 xterm 的输入、Ctrl-C、中文文本插入、搜索、粘贴预览、认证清空、诊断与手机辅助键；它要求测试环境中可解析与浏览器缓存匹配的 Node `playwright` 包，Python Playwright 安装不会自动提供这个 Node 包。Python 与 Node 的包版本号相同也不保证内置浏览器 revision 相同，分别使用各自安装的官方 CLI 准备浏览器；缺少指定 revision 时不要通过重命名缓存或软链接伪装匹配。中文文本插入不等同于真实系统 IME 验收，窄屏桌面浏览器不等同于移动真机。

```bash
# 仅在已准备 Node Playwright 及其浏览器的隔离测试环境运行。
CODEPIER_BROWSER_ENGINE=chromium node tests/vps_terminal_browser_smoke.mjs
CODEPIER_BROWSER_ENGINE=webkit node tests/vps_terminal_browser_smoke.mjs
```

将 `CODEPIER_BROWSER_EVIDENCE_DIR` 设置为本轮私有证据目录，例如 `.work/webssh-validation/screenshots`，可分别保存各引擎的布局和诊断截图。Chromium smoke 使用获准的真实浏览器剪贴板；WebKit 使用合成剪贴板事件检验实际 DOM 粘贴处理器，不把该结果算作操作系统剪贴板权限验证。全部 HTTP/WS 夹具测试仍不能代替真实浏览器与目标 VPS 联合验收。

tmux 实测需要安装真实 `tmux` 并允许创建 Unix-domain socket；测试使用专用临时目录和随机会话，缺少二进制或执行环境禁止该 socket 时会跳过。受限环境中 Chromium 也可能因系统 socket/进程能力限制无法启动。此类失败或跳过记录为环境限制与未验收项，不能用模拟测试结果替代真实浏览器、真实 tmux 或目标 VPS 的证明。

浏览器验收使用项目 `tests/browser_launch.py` 的现有启动策略，保留默认隔离和 actionability 检查。在允许运行浏览器与本地网络的 CI/专用测试机补齐验收；不要为通过测试关闭运行环境安全控制。

### 9.3 发布前应保留的证据

保留本轮源提交/SHA、锁文件检查、生成物检查、各测试命令的退出状态、通过/失败/跳过数量，以及浏览器/tmux/目标系统的实际验证范围。通过 CodePier 提交长任务只提交一次，保留 `operation_id`，pending 时继续读取原操作。证据充分后再按既有发布和部署流程推进；本手册自身不授权发布或重启服务。
