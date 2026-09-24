# Access Profiles：一个 ChatGPT 账号的多种连接身份

本功能是 Hub 源码变更，不代表线上服务或 ChatGPT 工具目录已更新。保持现有 Agent 的项目、本机目录、Shell、浏览器与桌面权限检查，不自动部署或重启 Agent。

## 两种明确的授权模式

**秘书等自用场景请选择动态角色授权**：Profile 关联角色，连线明确同意 `codepier.role_access` 后，角色的能力和项目清单可以持续增减（含新增项目），不再与首次项目清单取交集。入口及委派创建见 [动态角色授权](DYNAMIC_ROLES.md)。以下交集说明只针对固定授权。

旧固定 grant 不自动升级；关联角色也不改变它原来的同意范围。

## 固定授权使用流程

1. 登录管理面板，进入侧栏 **访问 Profiles**。
2. 建立有明确用途的身份，例如 `NewAPI Dev`（指定项目 + read/write/execute）、`Codex Review`（指定项目 + read）。`computer` 必须按需明确选择。
3. 在 ChatGPT 的 CodePier 应用连接设置增加另一个连接；OAuth 确认页面选择 **以哪个 Profile 连接**。下面只显示 Profile 与客户端申请范围共同允许的权限和项目，仍可进一步缩小。
4. 重新连接、刷新凭据或升级同意范围时选择原 Profile，不要新建一个同名身份。Profile 的不透明 ID 保持不变，显示名称可以修改。
5. 可以要求读取 `get_profile()` 确认当前身份；完整和编码 MCP 目录均提供 `get_access_context()`，显示当前有效 scopes 和可见项目别名，不返回密码、Token、主机根路径或其他连接。

ChatGPT 的多账户入口和显示取决于当前宿主。服务端提供标准身份工具，不保证每个第三方宿主都实现相同 UI。上线后需重新读取工具目录并做真实 ChatGPT 连接验收。

## 权限交集，不是新的超级权限

对于绑定 Profile 的**固定模式**连接：

```text
有效 MCP 权限 = 原 grant 明确同意的 scopes/projects ∩ 当前 Profile 上限
实际执行 = 有效 MCP 权限 ∩ Hub 项目策略 ∩ Agent 本机策略 ∩ 工具检查
```

Profile 增加项目或 execute/computer **不会超出旧 grant 原来的同意范围**。新增权限需要重新 OAuth 同意或新建 PAT。缩小 Profile 会影响后续请求和尚未派送的操作；再次放宽上限只能恢复原 grant 曾经同意的能力。停用 Profile 会令其所有绑定连接的 Bearer 认证和 Token 兑换/刷新失败。重新启用不会复活已经撤销的 grant。

默认只勾 read，不默认授权全部项目。仍保留明确的 **全部现有及未来项目** 选项；Profile 和原 grant 都同意这一选项时才涵盖未来项目。已有 1.13 版本的批量扩大连接范围设置只作用于传统 OAuth grant，不作用于 Profile 连接。传统 grant 的项目编辑接口拒绝修改 Profile grant，避免绕过原来的同意范围。

**停用不是取消命令。** 已经被 Agent 接收或结果不明的操作继续使用原回执恢复；不会自动重放、杀进程或回滚。确实需要停止任务时使用相应停止入口并检查原结果。

## 不把聊天当作安全边界

同一 ChatGPT 账号可能同时拥有多个连接。Profile 确保使用 A 凭据不能访问 B 未授权的项目，但**不能保证某个 ChatGPT 聊天或 Project 只能选择某一个连接**。

聊天标题、Project 名称、模型提供的 profile_id、workspace_id，以及 MCP 的 `?profile=coding` 都不是身份凭据。`?profile=coding` 只选择精简工具目录，与 Access Profile 完全无关。服务器只从经过认证的 Token 读取身份。

文件工具保留项目路径约束；完整 Shell、允许执行的本机任务、语言服务和桌面应用不是操作系统沙箱。需要跨项目强隔离时仍需使用独立 OS 用户、容器或虚拟机及相应凭据隔离。

## 身份和历史记录分开

`access_profiles.id` 使用随机、不可编辑、不可回收的 `prf_…` 标识。管理页支持创建、编辑、停用和重新启用，不提供删除后重用 ID。名称在同一 owner 内唯一。创建使用幂等键；更新使用乐观版本检查；更改均写入审计。

OAuth/PAT 的 `grants.profile_id` 绑定身份；固定模式使用 Profile 上限，角色模式绑定 `role_id` 并读取实时规则。原来的 `grant_id` 仍然拥有操作、工作流、审阅、浏览器租约和桌面会话。

**两个连接即使选同一个 Profile，也不会因此互读原 grant 的私有操作，或接管它的桌面会话。** 重新连接保持身份 ID，不等于重新连接后自动继承旧 grant 的历史。面板管理员仍可通过原管理接口查看记录。

## 旧数据兼容与升级

Hub 数据库增量迁移到 schema 7，包含 `access_profiles`、`access_roles` 及可空身份/角色绑定。旧 grant 默认保留 fixed 模式，不修改已有 grant 的 scopes、projects、Token、操作或 owner。迁移可以重复运行，不更换 `master.key`。

旧连接继续原样工作；其 `get_profile` 返回原 owner 的持久不透明账号 ID，不把每个旧 Token 伪装成不同的新 Profile。需要多个可辨识用途时，新建 Profile 并重新授权；不会静默将旧连接合并或重绑定。传统连接仍可使用原 PAT/OAuth 确认流程。

升级前备份 Hub 数据库和对应主密钥。新 schema 下已有 Profile 时，不要把旧 Hub 代码直接接回升级后的数据库；回退须使用配套的旧数据库备份。

## API 和 MCP

管理 API 仅接受原面板管理员会话，写操作还要求原 CSRF / Origin 校验：

- `GET /api/access-profiles`、`GET /api/access-profiles/{id}`
- `POST /api/access-profiles`：label、scopes、projects 或 all_projects、enabled、可空 role_id、idempotency_key
- `PUT /api/access-profiles/{id}`：同上策略字段及 expected_version

OAuth 同意 `/api/oauth/requests/{id}/decide` 和 PAT 创建 `/api/grants` 可附加 `profile_id`、`profile_version`。绑定在同一个数据库事务中重新检查，过期页面、停用或跨 owner Profile 都会被拒绝。不能在 Token refresh 中切换 Profile。

`get_profile` 是空参数、只读、非破坏性、非外部副作用的已认证 MCP 工具，标记 `_meta["openai/profile"] = true`。成功响应使用标准 schema：

```json
{"id":"prf_<opaque persistent ID>","name":"CodePier","nickname":"NewAPI Dev"}
```

`structuredContent` 和 JSON 文本返回同一个对象。不接受 profile/account selector。错误通过 `isError` 和文字返回，不把错误伪装成 profile 对象。固定目录保留原工具集合；动态角色目录额外提供 devices_list 和 projects_create，完整与编码模式均支持。另保留原 app-only 工作区读取工具。

官方接口参考：<https://developers.openai.com/plugins/build/auth>。

## 验证

```bash
python -m pytest -q tests/test_access_profiles.py tests/test_continuous_access.py \
  tests/test_audit_api.py tests/test_audit_store.py tests/test_audit_runtime.py \
  tests/test_coding_workflow.py tests/test_bridge.py
python -m pytest -q tests/test_access_profiles_ui.py
```

UI 用真实临时 Hub/Agent、Chromium/WebKit、桌面/手机尺寸测试，不使用生产账号。需要显式使用本机 Chromium 时设置 `CODEPIER_TEST_CHROMIUM_EXECUTABLE`；只跑 Chromium 不能声称 WebKit 或真实 ChatGPT 已验收。测试结果应随实际提交记录。
