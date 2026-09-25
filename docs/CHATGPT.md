# 连接 ChatGPT

本项目的 Hub、Agent 和 stdio 桥接器不需要模型 API Key。原生 CLI 的账号，以及官方 Tunnel 的运行凭据，是各自独立的授权。本文不表示已访问你的账号、创建 Tunnel 或部署服务。

工具目录统一为九个入口，浏览器和桌面包含在内；其他能力通过各工具的操作按需发现。旧工具名已移除，更新后需要刷新客户端工具目录。调用示例见 [九个 MCP 工具](CORE_TOOLS.md)。

## 两条接入路线

公开接入使用可信 HTTPS，MCP URL 为 `https://你的域名/mcp`。私有接入可使用官方 Secure MCP Tunnel，由服务器上的 tunnel-client 出站连接 OpenAI，并访问本机桥接器；不要求家里的 Agent 开放入站端口。不要把裸公网 HTTP 或服务器的 `127.0.0.1` 当成 ChatGPT 能直接访问的公开地址。

2026-09-24 核对的 [OpenAI 接入文档](https://developers.openai.com/plugins/deploy/connect-chatgpt) 说明：在 Settings → Security and login 打开 Developer mode；进入 Plugins，以加号创建连接，选择公开 MCP URL 或 Tunnel，并核对发现的工具。可用性受账号和工作区策略影响。私有 Tunnel 的开发者模式连接不能替代公开插件提交要求的 HTTPS 入口。

## 路线 A：HTTPS 与 OAuth

先完成 Hub、Agent 和项目映射，并在面板读取一个测试文件。使用已有可信 HTTPS 反向代理，或仓库的 `deploy/compose.https.yml`、`deploy/Caddyfile` 示例。域名、证书、网络可达性需在目标环境验证。

在 `.env` 保留正确的 `COMPOSE_FILE` 组合，后续更新继续使用同一组合。在“系统设置 → 公开地址”填写 HTTPS 基地址，不加 `/mcp`；数据库中已保存的公开地址优先于环境变量。只信任真实代理来源，不用通配信任绕过同源检查。

在客户端创建连接，选择 OAuth。CodePier 实现授权码、S256 PKCE、DCR 公共客户端 `token_endpoint_auth_method=none`，不实现 CIMD、private_key_jwt 或 client_secret_basic；采用服务实际支持的认证方式。跳转面板后登录、核对项目范围和工具权限，再明确确认。授权码及回调地址精确匹配；不要为解决回调失败放行任意域名。

## 路线 B：官方 Tunnel 与本地 stdio 桥接器

在面板“MCP 接入”创建限定范围 PAT，令牌仅显示一次。先给读取权限，确有需要再授予写入、执行或独立桌面权限。下面是在 Hub 所在服务器、原安装用户下准备桥接器的示例，路径按实际安装调整：

```bash
cd /opt/codepier
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-bridge.txt
mkdir -p private
chmod 700 private
umask 077
# 用本机编辑器把 CodePier PAT 写入文件，避免进入命令历史。
nano private/token.txt
chmod 600 private/token.txt
cat > private/bridge.env <<'ENV'
CODEPIER_HUB_URL=http://127.0.0.1:8765
CODEPIER_TOKEN_FILE=/opt/codepier/private/token.txt
ENV
chmod 600 private/bridge.env
```

桥接器入口是 `deploy/mcp-stdio.sh`。它给本机 Hub 的 `/mcp` 请求附加 PAT，拒绝权限过宽的令牌文件；不是关闭公网鉴权。初始化并调用 `tools/list` 可验证发现结果，工具数量和分页以响应为准。

按 [官方 Secure MCP Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 安装 tunnel-client、取得运行凭据并关联目标工作区。创建和运行 Tunnel 需要相应平台权限，工作区开发者模式权限另行检查。客户端需要出站 HTTPS 和到本地桥接器的可达性。先查看 `tunnel-client help quickstart`，将 named stdio profile 的 MCP command 设为实际桥接器绝对路径，再运行该 profile 的 doctor 和 run。运行时 OpenAI Key 与 CodePier PAT、管理员密码、Agent 密钥不能混用；不得写入仓库或公开日志。

在 Plugins 的连接方法中选择 Tunnel，选择对应条目或填写真实 tunnel_id。列表不可见时检查工作区关联和使用权限；不要通过更改 CodePier 认证策略规避平台权限。

## 新增项目无需反复重新连接

在“系统设置 → MCP 默认授权”可以默认预选全部现有及未来项目，以及读取、写入和执行。新应用仍需明确确认，不超过应用申请范围；桌面控制独立选择。

已有 OAuth 连接可明确勾选“同时将全部项目范围应用到已有有效 OAuth 连接”，确认保存。此操作不更换凭据、不扩展工具权限、不延长有效期、不恢复撤销授权，也不更改 PAT。单个 OAuth/PAT 可以在“MCP 接入 → 调整项目范围”修改，原令牌继续使用。关闭默认值只影响后续预选；收回已有权限需要单独缩小范围或撤销连接。

项目映射、令牌权限、Agent 本机执行能力仍分别检查。选择未来项目范围不代表客户端会自动更新工具目录或免除写操作确认。

## 元数据刷新与验证

工具名、描述、模式、认证或 UI 资源改变后，先部署相应源码，再在开发者模式连接中选择 Refresh，核对实际元数据并在新会话中验证。官方发布插件的更新流程与本地开发连接不同，以 [官方刷新说明](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata) 为准。新增项目范围与工具目录刷新是两件事。

验收从只读开始：发现项目，读取 README，再对一个明确的测试文件做带 SHA 检查的修改，最后执行已授权任务。回到面板核对项目、操作编号、审计和真实退出码。使用本地测试客户端通过不等于真实 ChatGPT 账号验收，也不构成官方兼容性认证。

收到 pending、queued 或 reconnecting 后继续查询原 operation_id；响应丢失按原幂等键查找。工具调用超时不能证明远端没有执行，不要生成新键盲目重做。更多恢复步骤见 [FAQ](FAQ.md) 和 [长操作](LONG_OPERATIONS.md)。
