# 面板用户流程体验审查（二）：协作中心、CLI 会话、VPS、开发任务

接 [UX_FLOW_REVIEW.md](UX_FLOW_REVIEW.md)，基线同为 `main@ba09fd3`（v1.33.2）。
本篇只看四个页面：协作中心（`collaboration-room.js`）、CLI 会话（`chat*.js`）、VPS（`vps.js`）、开发任务（`workflows.js`）。

协作中心已有一份重构设计 [COLLABORATION_CENTER_REBUILD_DESIGN_2026-11-02.md](COLLABORATION_CENTER_REBUILD_DESIGN_2026-11-02.md)（下称“11-02 设计”）。本篇不另起方向，第 1 节按“设计要求 → 当前代码 → 差距”逐项对照，再给改法。

---

## 0. 结论先行

| # | 问题 | 位置 | 优先级 |
|---|---|---|---|
| 1 | 新房间点“添加 dot”会被弹回权限页，提示“请先在此保存房间权限”。新建房间表单只有名称，没有项目选项；但创建接口其实已经接受 `projects` 参数 | `addMemberPanel`、`createPanel`、`submitForm` | P0 |
| 2 | 权限设置：每个项目一行 3 个复选框 + 执行位置 + 有效期两个控件 + 一句“这不是当前已授予权限”；每个分区 6 个批量按钮，房间、微信、Telegram 三个分区共 18 个；Telegram 未接入，也照样整块显示 | `renderPermissions`、`permissionRows` | P0 |
| 3 | 微信向导要确认三次：扫码后“确认扫码的是你”，范围页勾选确认，收到配对码后“确认是你本人发送的”。11-02 设计写的是“一次最终确认” | `renderChannel`、`channelCopy` | P0 |
| 4 | 微信确认页给用户看原始 `scanner_id`、`pending_peer_id`，用户没法核对一串 ID | `renderChannel` | P0 |
| 5 | CLI 会话：选 Codex 还是 Claude 藏在齿轮“会话设置”里；选项目有 3 个入口；删除前必须先停止，还要等进程退出 | `chatPage`、`chatNewSession`、`chatDelete` | P0 |
| 6 | 新建 VPS 默认“严格验证主机密钥”，而新服务器在 Agent 上没有记录，第一次检查注定失败；检查连接要先分配项目，再选项目，再点“开始检查” | `vpsEdit`、`vpsCheck` | P0 |
| 7 | 开发任务“记录进度”要手动粘贴操作编号；新建任务要先等“核对连接”，否则不让提交 | `workflowEdit`、`workflowCreate` | P1 |
| 8 | 协作中心主界面：标题区三处重复显示项目数；工作列表接在消息流末尾；消息流里有“消息窗口 50 / 200 条 · 历史按页读取”这类内部提示；成员下拉框选项是“名称 · #ab12cd34 · 可以协作” | `patchHeader`、`patchWorks`、`patchWindowHints`、`patchComposer` | P1 |
| 9 | 状态标签过多：dot 成员和微信共用一张 16 种的阶段表，工作 21 种（含 3 组同义词），投递 17 种 | `stages`、`workLabels`、`deliveryLabels` | P1 |
| 10 | 聊天页 ⌘K 和全局 ⌘K 绑在两个不同的面板上（`chat-chrome.js chatInteractionKey`、`ui.js:600`） | | P1 |
| 11 | “工作 / 开发任务 / CLI 会话 / 任务衔接”四个名字指向三套任务系统，页面之间没有互相说明 | 导航、`workflows.js`、`collaboration-room.js` | P2 |
| 12 | 文案：`collaboration-room.js` 一个文件里“核对”出现 61 次、“原操作”17 次、“回执”12 次；`#` 加短 ID 的标识出现在 7 处界面上 | 全部四个页面 | P1 |

---

## 1. 协作中心

### 1.1 和 11-02 设计的差距

| 11-02 设计要求 | 当前代码 | 差距 |
|---|---|---|
| §4.1 新建房间“只有名称必填，可选项目支持多选” | `createPanel` 只有名称字段；`submitForm` 却在读 `data.getAll('project')`，说明这个字段原本计划有，后来被删了 | 补回项目多选 |
| §4.2 “房间顶部只有‘项目 N’入口” | 顶部有“权限设置”按钮，旁边是一个不能点的“项目 N”文字，副标题里还列了一遍项目名 | 三处合成一个可点击的入口 |
| §4.2 “关联已有项目不要求房间先有 dot” | 满足 | — |
| §4.3 添加 dot 时“填名称，确认参与范围” | 房间权限没保存过（`configured=false`）时，`addMemberPanel` 直接跳到权限页报错 | 不应拦截（见 1.3） |
| §4.3 “入口首先展示‘已连接的 dot’和‘连接新 dot’” | 默认选中“新 dot”，切到“已有 dot”才去加载列表 | 有可复用的 dot 时默认展示它们 |
| §4.4 微信“扫码 → 确认本人 → 完成连接”，“一次最终确认” | 四步进度条，三次确认 | 合并确认（见 1.5） |
| §5.1 “内部队列和订阅数量放到详情” | 消息流里显示“消息窗口 x / y 条”“工作窗口 x / y 项 · 列表不等于完整历史” | 删除 |
| §5.1 “其他 dot 的过程默认收在关联工作卡里” | 工作列表整块接在消息流末尾，不跟消息关联 | 改位置（见 1.6） |
| §5.4 “一个对象同时只显示一个主要状态、一个阻塞原因、一个主要操作” | 工作卡同时显示状态、原因、投递状态；投递详情展开后有原投递 ID、`reason_code`、尝试次数 | 收敛（见 1.7） |
| §3 “技术细节只在诊断抽屉展开” | 工作详情的“步骤与原操作”默认展开（`<details ... open>`），每步都带“步骤 #xxxx”“原操作 #xxxx” | 默认收起，去掉 ID |

### 1.2 首次使用路径

| 阶段 | 现在 | 改后 |
|---|---|---|
| 建房 | 新建 → 填名称 → 创建 | 新建 → 填名称、勾项目 → 创建 |
| 配项目 | 点“权限设置” → 每个项目勾选项目、读取、修改、执行、执行位置，调有效期 → 保存 | 已在建房时完成；需要修改或执行权限时再进来改 |
| 加 dot | 成员 → 添加 dot →（如果权限没保存过，被弹回权限页）→ 填名称 → 添加并连接 → 复制加入说明 | 添加 dot → 填名称 → 复制加入说明 |
| 连微信 | 连接微信 → 扫码 → 确认扫码身份 → 确认范围（勾选）→ 复制配对码发送 → “我已发送，检查状态” → 确认是本人发送 | 连接微信 → 扫码 → 确认范围 → 发送配对码 → 自动完成 |
| 合计 | 约 20 次点击，权限页一次要处理几十个控件 | 约 9 次点击 |

### 1.3 建房时选项目，添加 dot 不再被拦

后端判断“权限是否已配置”的依据是 `room_permission_policies` 表里有没有这个房间的记录（`room_permissions.py`：`'configured': bool(row)`）。新房间没有记录，所以第一次添加 dot 一定会被拦。

**首选（后端）**：`rooms.create` 在同一事务里写入一条权限记录。没选项目就写空范围，选了项目就写“只读”，微信跟随房间。这样新房间永远是“已配置”，前端不需要特殊处理。旧房间（`legacy`）仍走迁移，不受影响。

**过渡（只改前端）**：

`createPanel` 加入项目多选，默认不勾选：

```js
function createPanel() {
  const projects = (S.projects || [])
    .map((p) => `<label class="cr-choice"><input type="checkbox" name="project" value="${E(p.id)}"><span>${E(p.alias)}</span></label>`)
    .join('');
  openPanel(
    'create',
    '新建房间',
    `<form data-cr-form="create">
      ${field('名称', '<input name="title" required maxlength="120" placeholder="例如：产品工作室" autofocus>')}
      ${projects ? `<fieldset class="cr-create-projects"><legend>项目（可以之后再加）</legend>${projects}</fieldset>` : ''}
      <button class="cr-button cr-primary" type="submit">创建</button>
    </form>`,
  );
}
```

`submitForm` 的 `create` 分支在建房成功后，紧接着写一次权限。即使没选项目也写，这样房间就是“已配置”：

```js
const chosen = data.getAll('project');
const result = await mutation('POST', base + '/rooms', {
  title: data.get('title').trim(),
  projects: chosen.map((project) => ({ project, environment_id: 'production' })),
});
if (!result) return;
const room = result.room || result;
const readOnly = (project_id) => ({
  project_id,
  environment_id: 'production',
  capabilities: ['read'],
  execution_targets: [],
  lifetime: { mode: 'until_revoked', expires_at: null },
});
await mutation('PUT', store.path(room.id) + '/permissions', {
  expected_version: 0,
  scope: { project_ids: chosen, project_scopes: chosen.map(readOnly) },
  channels: {
    weixin: { mode: 'follow_room', scope: { project_scopes: [] } },
    telegram: { mode: 'follow_room', scope: { project_scopes: [] } },
  },
}).catch(() => {}); // 失败也不影响房间本身，用户之后还能进权限页手动保存
```

需要确认后端在没有记录时接受 `expected_version: 0`（`view()` 里没有记录时返回的 `version` 就是 0，按理应该一致）。

`addMemberPanel` 里的拦截改为：非旧房间直接按当前预览范围静默保存一次，然后继续；只有旧房间保留跳转。

```js
if (!policy.configured) {
  if (state.room.legacy) {
    await permissionsPanel();
    panelError('旧房间先确认一次项目权限。');
    return;
  }
  await mutation('PUT', store.path(roomId) + '/permissions', {
    expected_version: policy.version,
    scope: policy.scope,       // 就是权限页显示的“保守预览”，不扩权
    channels: Object.fromEntries(
      Object.entries(policy.channels || {}).map(([k, v]) => [k, { mode: v.mode, scope: v.scope }]),
    ),
  });
}
```

添加 dot 面板本身：

- 打开时就加载已验证的连接（现在要切到“已有 dot”才加载）。有结果时，列表放在最上面，“连接新 dot”放在下面。
- 三段说明（`cr-info` ×2、`cr-muted` ×1）合成一句：“加入后使用房间的项目权限。”

### 1.4 权限设置：每个项目一个下拉框

现状（`permissionRows` / `permissionSection` / `renderPermissions`）：

- 每个分区 6 个批量按钮：全选项目、全选权限、全选读取、全选修改、全选执行、清空选择。
- 每个项目一行：项目复选框、读取 / 修改 / 执行 3 个复选框、执行位置复选框、“项目 / 宿主可授予上限：…这不是当前已授予权限”、有效期下拉框、截止时间输入框。
- 项目名后面一律拼上“· production”。
- 微信、Telegram 各一个分区，结构和房间分区完全一样。Telegram 的 `transport_supported` 是 `false`（后端写死只有微信为 `true`），也照样整块显示。
- 顶部 2 段说明，每个渠道 1 段，底部 1 段，外加一个“重新载入（放弃未保存选择）”。

改为：

| 控件 | 改后 |
|---|---|
| 项目行 | 项目名 + 一个下拉框：不加入 / 只读 / 读写 / 读写和运行。超出这台电脑上限的选项置灰，鼠标悬停时说明原因 |
| 环境名 | 只有非 `production` 时才显示 |
| 执行位置 | 只有选了“读写和运行”才出现，默认全选，收在一行“运行位置：全部（2）”里 |
| 有效期 | 顶部一个全局下拉框：直到撤销 / 到某天。单个项目要单独设置，从行尾“⋯”进入 |
| 批量按钮 | 只留一个：“全部设为…”下拉框 |
| 微信 | 一个复选框“微信使用和房间相同的权限”，默认勾选；取消勾选后才展开同样的表格 |
| Telegram | `transport_supported === false` 时不显示 |
| 说明文字 | 只留一句“保存后生效。” |

**建议代码**：只换掉控件，提交给后端的结构不变。

```js
const LEVELS = [
  ['', '不加入'],
  ['read', '只读'],
  ['write', '读写'],
  ['execute', '读写和运行'],
];
const capsOf = (level) =>
  ({ '': [], read: ['read'], write: ['read', 'write'], execute: ['read', 'write', 'execute'] })[level];
const levelOf = (caps = []) =>
  caps.includes('execute') ? 'execute' : caps.includes('write') ? 'write' : caps.includes('read') ? 'read' : '';

function permissionRow(project, rule) {
  const available = project.capabilities || [];
  const current = levelOf(rule?.capabilities);
  const env = project.environment_id && project.environment_id !== 'production' ? ' · ' + project.environment_id : '';
  const options = LEVELS.map(([value, label]) => {
    const ok = capsOf(value).every((cap) => available.includes(cap));
    return `<option value="${value}" ${value === current ? 'selected' : ''} ${ok ? '' : 'disabled'}>${label}</option>`;
  }).join('');
  return `<div class="cr-perm-row" data-permission-row data-project-id="${E(project.project_id)}" data-environment-id="${E(project.environment_id || 'production')}">
    <span>${E(project.name || project.project_label)}${E(env)}</span>
    <select data-permission-level aria-label="${E(project.name || project.project_label)} 的权限">${options}</select>
  </div>`;
}
```

`collectPermissionScope` 里把读复选框改为读下拉框：

```js
const level = group.querySelector('[data-permission-level]').value;
if (!level) continue;
const capabilities = capsOf(level);
```

报错信息里不要再出现原始项目 ID。现在的写法是 `'项目 ' + project.project_id + ' 的有效期已过期或无效…'`，应改用项目名称。

### 1.5 微信向导：只确认一次

现在的阶段和确认点：

| 阶段 | 用户要做的 | 问题 |
|---|---|---|
| `choose_account` / `scan` | 选已有账号或扫码 | — |
| `confirm_account` | 看“扫码身份：wxid_xxx…”，点提交（隐藏字段 `confirm_scanner_id`） | 用户刚在手机上点过“确认登录”，这里又要确认一次，而且显示的是一串看不懂的 ID |
| `confirm_binding` | 勾选聊天 / 交办工作，点提交 | 这是唯一真正需要用户做决定的一步 |
| `pairing` | 复制配对码发过去，再点“我已发送，检查状态” | 页面本来就在自动轮询，这个按钮多余 |
| `confirm_owner` | 看“发送配对码的联系人：xxx”，再点提交 | 又一次确认，而且又是原始 ID |

改法：

1. **账号确认并入范围确认**。`confirm_account` 只在两种情况下停下来：`re_pair_required`（这个微信在别的房间登记过），或 `existing_account` 冲突。其余情况，扫码成功后直接进入 `confirm_binding`，账号名称显示在那一页的摘要里。这和 11-02 设计“一次最终确认中看清微信身份和联系人”一致。需要改后端流程。
2. **本人确认前移**。在 `confirm_binding` 写明“配对码从哪个微信发来，就绑定哪个微信”。配对码收到后直接完成。只在 `pairing_feedback.state === 'different_peer'`（恢复连接时发码的不是原联系人）时才停下来。
   这是安全上的取舍：配对码 15 分钟有效，只显示在已登录的面板上。如果维护者认为仍需要保留第二次确认，至少要把 ID 换成昵称。
3. **配对阶段不显示提交按钮**。`quiet` 分支里的“我已发送，检查状态”删掉，只留倒计时和“复制”按钮。
4. **身份显示**。有昵称就显示昵称，没有就显示脱敏 ID（前 4 位 + 后 4 位），不显示完整原始 ID。
5. **完成页**。现在的完成页有：信息表、“项目权限 / 修改项目权限”一节、“当前项目权限（只读）”折叠区、附件外发时长估算、一段关于撤销工作许可的说明、次要按钮。改为：

```
微信已连接
微信：张三的机器人 · 房间：产品工作室
在微信里交办工作：已允许   [关闭]
[修改权限]  [暂停]  [解除]
```

“仅撤销工作许可”就是上面那个“关闭”。附件时长估算只在实际发送附件时显示。

**建议代码**（`renderChannel`）：

```js
const shortId = (value) =>
  !value ? '' : String(value).length <= 10 ? String(value) : String(value).slice(0, 4) + '…' + String(value).slice(-4);
const who = (nickname, id) => nickname || shortId(id);

// confirm_account
facts([['微信', who(qr.scanner_nickname, qr.scanner_id)], ['已登记为', qr.existing_account?.label]]);

// pairing：删掉按钮
if (stage === 'pairing') showForm = false; // 自动轮询已覆盖

// ready：工作许可做成一行开关
if (stage === 'ready')
  parts.push(`<p class="cr-wx-toggle">在微信里交办工作：${flow.work_authorized ? '已允许' : '未开启'}
    ${btn('channel-action', flow.work_authorized ? '关闭' : '开启', `data-channel-action="${flow.work_authorized ? 'revoke_tasks' : 'approve_tasks'}"`, 'cr-text-button')}</p>`);
```

`scanner_nickname` 是假定字段。如果微信接口能拿到昵称，由后端补上；拿不到时就显示脱敏 ID。

另外，打开“连接微信”面板时，`weixinPanel` 会直接 `POST .../channels/weixin/connect`。只是看一眼也会建一个连接流程。建议先查房间里有没有进行中的流程，有就直接打开；没有再在用户点“扫码登录”时创建。优先级低。

### 1.6 房间主界面

**标题区**（`patchHeader`）：现在显示房间名、状态标签（如“2/3 个 dot 在线”）、副标题（项目名列表，或“N 个项目 · M 个成员”），再加一个独立的“项目 N”文字，项目数出现了三次。改为：

```
产品工作室
MCP、Lumen · 2/3 个 dot 在线
[项目 2] [成员 ●●●] [微信 ●] [⋯]
```

“项目 2”就是原来的“权限设置”，改名后可以点击。“⋯”里放：房间要点（原“当前上下文”）、诊断、暂停、归档、重命名。

“当前上下文”现在藏在房间设置里，是一个二级入口。它是房间的长期记忆，应该和项目、成员同级。建议改名“房间要点”，放进“⋯”的第一项。

**工作列表位置**（`patchWorks`）：消息流的结构是 `messages → pending → work-list`，工作列表接在最新消息下面。工作一多，最新消息就被推到上方，看不见。改为：

- 进行中的工作做成输入框上方的一条横条：“进行中 2 · 等你回复 1”，点开是右侧详情面板。
- 已完成的工作不在消息流里常驻，只在对应消息下方显示一行“已完成：修复上传”。

**消息流里的内部提示**（`patchWindowHints`、`patchWorks`）：删掉“消息窗口 x / y 条 · 历史按页读取”“工作窗口 x / y 项 · 列表不等于完整历史”“当前历史窗口到此结束，后续消息尚未载入。”。保留“更早的消息”按钮就够了。

**接收人下拉框**（`patchComposer`）：现在每个选项是 `名称 · #ab12cd34 · 可以协作`。改为只显示名称；只有重名时才加短 ID；状态用选项前的圆点表示。

**消息投递**（`deliveryMarkup`）：现在每条带微信回复的消息都有一个折叠块，里面是“原投递 #xxx · 原消息 #xxx”、分项列表、`reason_code`、“已记录尝试 N 次”，以及“平台接受不等于对端送达或已读…”。改为消息底部一行小字：

```
微信 · 已送达              （正常时）
微信 · 发送失败 [重试]      （失败时；分项详情放进“重试”旁的“详情”）
```

**旧房间**：顶部横幅、空状态里的迁移清单、迁移面板三处说的是同一件事。代码注释已经写了“one actionable checklist instead of three copies”，但横幅仍然和清单同时显示。没有消息时只显示清单；有消息时只显示横幅。迁移面板里的“我已核对……确认启用”复选框，加上“启用新房间界面”按钮，等于点两次。服务端已经判断过 `eligible`，复选框可以删掉，保留一个按钮，点击时再弹一次确认框。

### 1.7 状态标签收敛

后端阶段不用改，只改显示层。

**dot 成员**（列表、下拉框、头像旁）只用 4 种：

| 显示 | 对应阶段 |
|---|---|
| 连接中 | invited、joining、waiting_host、verifying |
| 在线 | ready_chat、ready_work（是否能工作在成员详情里说明） |
| 需要处理 | degraded |
| 已暂停 | paused |

成员详情页仍然显示完整阶段和下一步动作，与 11-02 设计 §4.3 的表格一致。

**工作**（卡片和横条）只用 8 种：

| 显示 | 对应状态 |
|---|---|
| 排队中 | queued、leased |
| 进行中 | running、verifying |
| 等你回复 | waiting_user、waiting_input |
| 等待中（附原因） | waiting_member、waiting_authorization、waiting_target、reconnecting、blocked、reconciling、unknown |
| 已暂停 | paused |
| 已完成 | succeeded、completed |
| 正在停止 | cancelling、stopping（不能显示成“已停止”，发出停止请求不等于已经停下） |
| 已停止 / 失败 | cancelled、failed、expired |

**投递**只用 4 种：发送中、已送达（accepted、owner_confirmed）、发送失败（rejected、failed、needs_attention、attention）、未确认（unknown，配“查看”按钮）。

### 1.8 协作中心文案对照

| 位置 | 现在 | 改为 |
|---|---|---|
| `readOnlyReason`（旧房间） | 原房间当前只读。历史、诊断和停止操作仍可使用；完成迁移并明确启用后才能继续协作。 | 这是旧版房间。处理完下面几项后就能继续使用。 |
| `readOnlyReason`（其他） | 房间当前只读，请先核对房间状态。草稿与原工作已保留。 | 房间已暂停或归档，恢复后才能发消息。 |
| 无房间空状态 | 事情，从这里开始 / 一个房间，一段持续的协作。 | 还没有房间。新建一个，把项目和 dot 放进来。 |
| 有成员空状态 | 想一起做点什么？/ 直接发消息，工作和进展都会留在这里。 | 发条消息开始。 |
| 无成员空状态 | 让你的 dot 加入 / 先连接成员，也可以先把目标写下来。 | 添加一个 dot，或者先把目标写下来。 |
| `createPanel` 说明 | 创建后在房间的权限设置中统一选择项目及读取、修改、执行权限。 | 删除（表单里已有项目选项） |
| 权限页顶部 | 这是本房间唯一的项目权限设置。房间成员统一使用下方范围；微信、Telegram 可跟随房间或使用自定义范围。更改仅在保存后生效。 | 保存后生效。 |
| 权限页（未配置时） | 以下是原有实际授权的保守预览，不会自动扩权。首次保存后统一使用此处设置。 | 删除（新房间不会出现；旧房间放在迁移清单里说明） |
| 权限页每行 | 项目 / 宿主可授予上限：读取、修改。这不是当前已授予权限。 | 删除。改为置灰选项的悬停提示：“这台电脑没开放运行命令” |
| 权限页渠道说明 | 跟随模式随房间设置同步变化；自定义范围不能超过……但不另设项目权限。 | 删除（复选框“微信使用和房间相同的权限”已经说明白了） |
| 权限页底部 | 收窄权限后，新的任务和后续步骤按新范围核验；已启动操作的结果仍需核对原回执。 | 收窄后，后续步骤按新范围执行。 |
| 权限页按钮 | 重新载入（放弃未保存选择） | 放弃修改 |
| 添加 dot | （3 段说明） | 加入后使用房间的项目权限。 |
| 已有 dot 列表 | 这里只列出当前账号已完成往返验证的原连接。 | 删除 |
| 已有 dot 为空 | 还没有其他房间的已验证连接。先添加一个新 dot 并完成连接。 | 其他房间还没有连好的 dot。 |
| 成员详情（复用） | 请在已选择的原 dot 中完成本房间的独立接入与验证。此房间的参与范围单独生效。 | 把加入说明发给这个 dot。 |
| 成员详情（更换） | 新连接验证通过后才会替换当前连接。原成员和工作保持原来的身份。 | 新连接接通后才会替换旧连接。 |
| 成员详情标题 | 名称 · #ab12cd34 | 名称 |
| 投递说明 | 平台接受不等于对端送达或已读；本人确认会单独标明。 | 删除 |
| 投递说明 | 服务端已记录供应商送达证据；尚无已读证据。 | 已送达 |
| 待确认操作 | 仅按原请求键核对。若原操作未保存，当前只读状态会阻止执行；不会改用新的请求。 | 删除 |
| 待确认操作按钮 | 核对原操作 | 查看结果 |
| 发送未确认按钮 | 核对原消息 | 确认是否已发送 |
| 附件未确认按钮 | 核对原上传 | 确认是否已上传 |
| 工作详情（无操作） | 当前没有已记录的操作；不会把未开始显示为已完成。 | 还没开始执行。 |
| 工作详情（无验收） | 此工作未单列验收条件。 | 隐藏整个“验收条件”小节 |
| 工作详情（等待） | 先核对上面的原操作回执，确认结束后才能恢复或接替。 | 等上面的操作结束后才能接替。 |
| 工作不可读 | 原工作当前不可读，请刷新房间后核对。 | 这项工作已看不到，可能是权限变了。 |
| 房间要点 | 摘要保留原消息引用。纠正会回到房间输入框，由你写明新的事实。 | 点“纠正”后，在输入框写出正确内容。 |
| 房间要点标题 | 当前事实 · 版本 7 | 房间要点 |
| 来源按钮 | 原消息 #a1b2c3d4e5f6 · v3 | 来源（11:42） |
| 输入框纠正提示 | 正在纠正事实 #xxx · 版本 7 | 正在纠正：“{事实前 20 字}” |
| 微信首页 | ……登录只登记账号，不会自动开放权限或发送消息。 | 用手机微信扫码登录。 |
| 微信配对 | 用来证明这个私聊确实是你本人。发送后本页会自动进入下一步。 | 把配对码发到机器人私聊，这里会自动继续。 |
| 微信收件提示 | 延后消息保留在原连接，积压消化后自动核对；隔离消息不会执行任务。后续正常消息仍可接收。 | 有 N 条消息被拦下，不会执行。 |
| 微信撤销说明 | 可以仅撤销这个微信的工作许可，保留聊天连接。新的微信任务与后续步骤会停止准入；已启动操作继续核对原回执。 | 删除（改成一行开关，见 1.5） |
| 迁移面板 | 服务器已核对：当前没有阻止启用的连接。 | 可以启用了。 |
| 迁移完成提示 | 新房间界面已启用。历史、草稿和原工作已保留。 | 已切换到新版房间。 |

### 1.9 命名

- “协作中心”“协作室”“房间协作中心”三种叫法：README 写“协作室默认关闭”，又有 `docs/COLLABORATION_CHATROOM.md`。界面和文档统一叫“协作中心”。
- “dot”第一次出现时（新建房间后的空状态、添加 dot 面板）需要一句解释，说明它指什么。现在全站没有任何地方解释过这个词。
- “主责”可以保留，但同一个界面里不要同时出现“主责 dot”和“房间主责”两种说法。

---

## 2. CLI 会话

### 2.1 新建会话：项目和 CLI 一次选完

现在的路径：

1. 点“新对话”。侧栏、顶部“⋯”菜单、快捷操作面板、快捷键 ⌘⇧O 都能触发。
2. 弹出项目选择框：“这次在哪个项目工作？”，下面一行“选定项目后再写消息。现在不会启动 CLI，也不会离开或停止原会话。”
3. 选完项目，CLI 沿用上次的（`providerHint`）。想换成 Codex 或 Claude，要点输入框右下的齿轮，打开“会话设置”，再在“CLI”下拉框里改。
4. 模型在输入框左下角单独选。

选项目还有另外两个入口：会话设置里的“工作项目”下拉框，以及它旁边的“选择项目”按钮。项目页的项目卡上还有 Pi / Codex / Claude 三个按钮。

**改法**：

- 项目选择框顶部加一个三选一：Pi / Codex / Claude，默认是上次用的。选项目时一起确定 CLI。
- 输入框下方把“项目 · CLI · 模型”合成一个按钮，点开是一个面板，里面放项目、CLI、模型、思考强度、工作目录。删掉会话设置里的“工作项目”下拉框、“选择项目”按钮和“CLI”下拉框。
- 项目卡上的三个 CLI 按钮合成一个“对话”按钮，打开项目选择框并预选这个项目。

```js
// chatNewSession：选择框头部
dialog.innerHTML = `<header class="chat-project-sheet-head"><h2 id="chat-project-dialog-title">新对话</h2>
  <div class="chat-segmented" role="radiogroup" aria-label="CLI">
    ${['pi', 'codex', 'claude'].map((id) => `<label><input type="radio" name="cli" value="${id}" ${id === providerHint ? 'checked' : ''}>${chatProviderName(id)}</label>`).join('')}
  </div>
  <button type="button" id="chat-project-dialog-close" class="chat-square" aria-label="取消">×</button></header>
  <input id="chat-project-search" … placeholder="搜索项目">…`;

// choose()
const provider = dialog.querySelector('[name="cli"]:checked')?.value || providerHint;
await chatSwitch(null, project, { provider, cwd: '.' });
```

### 2.2 同一个设置散在多处

| 设置 | 出现的位置 |
|---|---|
| 模型 | 输入框“选择模型”；详情面板“已确认模型”“下一轮模型”“更换模型”；快捷操作“选择模型” |
| 思考强度 | 会话设置“思考”下拉框；详情面板“已确认思考”“下一轮思考” |
| 上下文用量 | 会话设置里一个叫“上下文”的按钮（实际打开的是详情面板的“概览”）；详情面板“上下文与用量” |
| 指令 | 会话设置“指令与技能”；详情面板“指令”标签页；输入 `/` 弹出的列表；快捷操作“指令与技能” |

统一做法：设置只在 2.1 那个合并按钮的面板里改。详情面板只展示当前值（不再区分“已确认 / 下一轮”，有变化时显示“下一轮改为 X”）。指令只保留输入 `/`，以及详情面板的“指令”标签页。

### 2.3 顶栏

现在：搜索、专注模式、会话详情三个按钮，加上“⋯”菜单里 12 项：新对话、搜索当前对话、会话详情（这两项是移动端的重复）、当前项目开发工具、测试与验收、任务衔接、重命名、恢复会话、导出完整 Markdown、导出完整 JSON、停止会话进程、删除网页记录。

改为：

- “⋯”里只保留：重命名、导出（二级菜单选 Markdown 或 JSON）、停止、删除。
- “恢复会话”只在会话已退出时出现，而且放在消息流底部，作为主按钮。
- 开发工具、测试与验收、任务衔接三项属于项目，不属于某个会话。移到项目页的“更多”菜单。

### 2.4 删除：停止和删除一步完成

现在 `chatDelete` 遇到运行中的会话会直接报错：“请先停止会话，确认进程退出后再删除记录”。用户要先停止，等进程退出，再回来删除。

`chatConnect` 的事件流会原地更新 `ChatUI.selected`（`chat.js` 中 `Object.assign(c.selected, JSON.parse(e.data))`），所以可以在同一个流程里等它退出：

```js
async function chatDelete() {
  const c = ChatUI, row = c.selected, v = chatView(), gen = c.generation, project = c.project;
  if (!row) return;
  const live = () => ['starting', 'running', 'stopping', 'orphaned'].includes(row.status);
  const running = live();
  const confirmed = await chatDialog({
    title: running ? '停止并删除这个会话？' : '删除这个会话？',
    body: (running ? '正在进行的回复会中断。' : '') + '电脑上的 CLI 历史和项目文件不受影响。',
    confirmLabel: running ? '停止并删除' : '删除',
    danger: true,
  });
  if (!confirmed || !chatCurrent(gen, v)) return;
  if (running) {
    if (v.stopSessionId !== row.id) {
      v.stopSessionId = row.id;
      v.stopReceipt = uid();
    }
    await chatAPI('stop', { id: row.id, receipt: v.stopReceipt }, project);
    chatStatus('正在停止…');
    const deadline = Date.now() + 30000;
    while (live()) {
      if (!chatCurrent(gen, v)) return;
      if (Date.now() > deadline) {
        chatStatus('会话还没停下来，稍后再删。', true);
        return;
      }
      await pause(1000);
    }
  }
  await chatAPI('delete', { id: row.id, confirm: row.id }, project);
  // ……后续与现有代码相同
}
```

`orphaned` 状态可能永远等不到退出，30 秒超时就是为这种情况准备的。

### 2.5 快捷键冲突

`ui.js:600` 把 ⌘K 绑到全局“快速前往”，`chat-chrome.js` 的 `chatInteractionKey` 又把 ⌘K 绑到聊天页的“快捷操作”。两个面板内容不同，在聊天页按 ⌘K 打开哪一个取决于监听器的顺序。聊天页的面板已经包含了全部导航页，所以建议：在聊天页时全局处理函数直接 return，由聊天面板接管。

```js
// ui.js 全局 ⌘K 处理函数开头
if (document.getElementById('chat-root')) return; // 聊天页的快捷操作已包含页面导航
```

### 2.6 侧栏和输入区

| 位置 | 现在 | 改为 |
|---|---|---|
| 侧栏头部 | “会话空间”+“Pi / Codex / Claude” | 删除 |
| 侧栏筛选 | 项目下拉框 + 搜索框 + 状态下拉框，外加“清除筛选” | 只留搜索框。项目和状态做成搜索框下方的两个小标签，点开再选 |
| 侧栏底部 | 快捷操作 ⌘K、项目管理、外观下拉框 | 只留快捷操作。外观已经在全局侧栏里有了，项目管理在主导航里也有 |
| 运行中的发送方式 | 下拉框“排队跟进 / 立即补充” | 改为“等这轮结束再发 / 插入当前回复”，默认前者 |
| 会话设置标题 | 会话设置 / 按你的工作方式调整 | 删除副标题 |
| 空状态 | “CLI 工作空间”+“今天，想推进什么？”+“选择项目并描述任务，与 Pi 一起推进工作。” | 去掉英文风格的 eyebrow；标题改为“{项目名} · {CLI}”，下面直接放三个快捷开头 |
| 两条提示条 | `chat-target-notice`（检查连接）和 `chat-catalog-notice`（重新加载）可能同时出现 | 合成一条，只显示优先级高的那个 |

### 2.7 CLI 会话文案对照

| 位置 | 现在 | 改为 |
|---|---|---|
| 新对话框 | 选定项目后再写消息。现在不会启动 CLI，也不会离开或停止原会话。 | 删除 |
| 停止确认 | 当前任务和排队消息会中断。对话历史与项目文件会保留。 | 正在进行的回复和排队消息会取消。 |
| 停止后 | 已请求停止，等待原生进程退出 | 正在停止… |
| 删除确认 | 删除网页记录？/ 仅删除这条网页会话记录，不删除本机原生历史或项目文件。 | 删除这个会话？/ 电脑上的 CLI 历史和项目文件不受影响。 |
| 恢复失败 | 会话进程尚未退出，请先核实节点状态。 | 会话还在运行，先停止再恢复。 |
| 发送未确认 | 上一条消息尚未确认，请重试原请求。草稿已保留。 | 上一条消息还没确认发出，点“重试”。 |
| 重试按钮 | 重试原请求 | 重试 |
| 审批 | 回答未被原生确认，请重试原回答核实。 | 回答没有送达，点“重试”。 |
| 审批按钮 | 重试原回答 | 重试 |
| 详情面板 | 关闭页面不会停止会话。模型参数只影响当前会话，不覆盖本机全局配置。 | 关闭页面后会话继续运行。 |
| 详情面板 | 等待原生统计 / 费用（原生统计） | 暂无数据 / 费用 |
| 全站 | “原生”（原生 CLI、原生进程、原生历史、原生指令） | 直接说“CLI”或删掉 |

---

## 3. VPS

### 3.1 新 VPS 第一次检查注定失败

`vpsEdit` 里主机密钥验证的默认值是“严格验证已有主机密钥”（`v.host_key_policy !== 'accept-new'` 时选中 `strict`）。下面的提示又写着：“首次连接且 Agent 尚无主机记录时，需要明确选择‘信任首次连接’。”也就是说，新加的服务器按默认值保存，第一次连接一定失败。用户要回去编辑，改选项，再保存，再检查。

两种改法，二选一：

- **新建时默认“信任首次连接，拒绝后续密钥变化”**。这和 `ssh -o StrictHostKeyChecking=accept-new` 的行为一样。编辑已有 VPS 时保持原值。
- **保持严格，失败时给一键修复**。检查返回“主机未知”时，在结果区显示指纹，并提供按钮“信任这个主机并重试”。这需要后端在错误里带回指纹。

前一种只改一行：

```js
const policy = v.host_key_policy || (id ? 'strict' : 'accept-new');
// <option value="strict" ${policy === 'strict' ? 'selected' : ''}>…
// <option value="accept-new" ${policy === 'accept-new' ? 'selected' : ''}>…
```

### 3.2 保存后直接检查

现在：保存 → 回到列表 → 点卡片上的“检查连接”（没分配项目时按钮是灰的）→ 弹窗里选“通过哪个项目连接” → 点“开始检查”。

改为：编辑框的主按钮改成“保存并检查”。保存成功后，在同一个弹窗下半部分直接跑检查。自动挑选已分配的项目中第一个在线、并且允许运行命令的；没有符合条件的项目，就只保存，并说明原因。

```js
async function vpsCheck(id, { autoRun = false, host = null } = {}) {
  // ……原有逻辑
  const projects = v.projects
    .filter((p) => p.mode === 'write' && p.allow_tasks)
    .sort((a, b) => Number(b.online) - Number(a.online)); // 在线的排前面
  // ……
  if (autoRun && projects.length && v.enabled) button.click();
}

// vpsEdit 保存成功后
const saved = await api('/api/vps' + (id ? '/' + id : ''), { … });
closeModal(dialog);
toast('VPS 已保存');
if (data.enabled && data.project_ids.length) await vpsCheck(saved.id || id, { autoRun: true });
```

检查弹窗里的“通过哪个项目连接”下拉框，只有在可选项目超过一个时才显示。

### 3.3 分配入口重复

同一个“VPS ↔ 项目”的关系，现在有四个地方可以改：编辑表单里的项目复选框、VPS 卡片上的“分配项目”按钮、VPS 卡片上的项目标签（点击后打开按项目分配）、项目卡上的“VPS 分配”按钮。

保留编辑表单（从 VPS 这一侧改），以及项目卡“更多”菜单里的“VPS”（从项目这一侧改）。删掉卡片上的“分配项目”按钮。项目标签只用于展示，不可点击。

分配列表里，没开放运行命令的项目现在只在后面标一句“· 未授权执行”。改为：勾选这种项目时，就地提示“这个项目还不能运行命令，VPS 检查会失败”，并附一个“去开启”链接（打开项目编辑）。

### 3.4 其他

| 位置 | 现在 | 改为 |
|---|---|---|
| 页头 | VPS 管理 / 服务器连接 / 保存连接，按项目分配。 | VPS |
| 顶部统计 | 全部连接 / 已启用 / 已分配项目三格 | 删除，并入筛选栏右侧的计数“3 台 · 2 台已分配” |
| 统计旁 | 密码保存在服务端，调用时无需再次提供。 | 移到密码输入框的说明里 |
| 卡片“认证”一行 | 已保存密码 | 删除（只有密码这一种方式，所有卡片都一样） |
| 页底帮助 | “如何在 ChatGPT 中调用”两段 | 留一句：“在 ChatGPT 里说‘SSH 到 {名称}，检查磁盘’。” |
| 保存未确认 | 保存结果待核实。请刷新 VPS 列表确认，不要反复新建；相同连接不会重复保存。 | 网络中断，不确定是否已保存。刷新列表看一下。 |
| 检查说明 | ……只读取主机、当前账号、系统和根目录磁盘用量，不修改服务器。 | 读取主机名、系统和磁盘用量。 |
| 检查进行中 | 检查已提交 · {操作 ID}。关闭窗口后仍可在操作审计中查看。 | 正在检查… |
| 检查失败 | ……；点击恢复将沿用同一请求，不创建重复命令。 | 检查没有完成。[重试] |
| 检查按钮 | 恢复本次检查 | 重试 |
| 删除确认 | 将删除连接及全部项目分配。不会删除服务器；已下发的命令不会自动停止。 | 服务器本身不受影响。 |
| 取消分配说明 | 取消分配会阻止尚未下发的命令；已下发或被 Agent 接收的任务不会自动终止，请查询原操作回执。 | 删除 |

---

## 4. 开发任务

### 4.1 定位

页面底部写着“这里只保存进度，不自动执行代码”。实际上，任务主要由 ChatGPT 通过 `workflows_create` / `workflows_update` 建立和更新，面板上手动建任务、记进度是次要场景。可页面的主操作仍是“新建任务”，详情页的主按钮仍是“记录进度”。

另外，全站有四个名字指向三套“任务”：

| 名字 | 是什么 | 在哪里 |
|---|---|---|
| 开发任务 | MCP 客户端记录的步骤进度 | 导航“开发任务” |
| 工作 | 协作中心里 dot 执行的工作 | 协作中心房间内 |
| CLI 会话 | 在 Agent 电脑上跑的 Pi / Codex / Claude | 导航“CLI 会话” |
| 任务衔接 | 把开发任务交给开发工具 | 开发任务详情、CLI 会话“⋯” |

建议：

- 导航“开发任务”改名为“任务进度”。页头一句话说明：“ChatGPT 记录的分步进度。”
- 列表页主按钮从“新建任务”降为次要按钮。
- 三套系统在各自详情页互相链接。例如协作中心某项工作调用了 workflow，就在工作详情里链接过去，而不是让用户自己去猜。

### 4.2 新建任务

现在：项目、交接对象（打开弹窗时异步“核对”可交接连接，附“重新核对连接”按钮和一行状态文字）、任务名称、要完成什么、检查清单模板（附“清单不自动执行命令”）。核对没完成时提交会被拦截：“请先核实所选项目当前可交接的连接。”

后端在建任务时本来就会再查一次权限（状态文字里自己写了“建立任务时会再次检查权限”），前端没必要拦截。

改为：

- 必填项只留“要完成什么”。任务名称默认取第一行的前 40 个字，可以改。
- 交接对象默认“仅面板管理”。列表在后台加载，加载完再填充下拉框，不阻止提交。删掉“重新核对连接”按钮。
- 检查清单模板默认“检查与修复”，收进“更多选项”。

```js
// workflowCreate 提交时
() => {
  const args = Object.fromEntries(new FormData($('#workflow-create-form')));
  if (!args.title?.trim()) args.title = args.goal.trim().split('\n')[0].slice(0, 40);
  if (!args.assignee_grant_id) delete args.assignee_grant_id;
  return args; // 权限交给后端判断
}
```

### 4.3 记录进度：选择操作，不手填编号

现在“本轮操作编号”是一个文本框，提示“多个编号用换行或逗号分隔”。用户要先去审计页找到操作，复制 ID，再回来粘贴。

后端已经有 `GET /api/operations?project=&status=succeeded&limit=`（`hub/api/activity.py`）。直接列出这个项目最近的成功操作让用户勾选：

```js
async function workflowEvidencePicker(w) {
  const r = await api(
    '/api/operations?' + new URLSearchParams({ project: w.project_id, status: 'succeeded', limit: '20' }),
  );
  const since = w.created || 0;
  const rows = r.operations.filter((op) => op.created >= since);
  return rows.length
    ? `<div class="check-list">${rows
        .map((op) => `<label class="check"><input type="checkbox" name="evidence" value="${esc(op.id)}"><span>${esc(op.tool)}<small>${esc(shortTime(op.created))}</small></span></label>`)
        .join('')}</div>`
    : '<p class="form-note">这个任务建立后还没有成功的操作。</p>';
}
// 读取时
evidence: $$('[name="evidence"]:checked', form).map((x) => x.value),
```

其他改动：

- “步骤状态”默认值从“进行中”改为“已完成”。在面板上手动记进度，最常见的就是标记某步完成。
- “操作”下拉框（记录步骤或断点 / 标记受阻 / 最终验收并完成 / 取消任务）改成四个按钮，点哪个就展开对应的输入项。
- 详情页底部现在有 4 个按钮：衔接到开发工具、测试验收、重新读取、记录进度。改为只留“记录进度”。“重新读取”改为自动刷新；前两个移到“⋯”。
- 详情页元信息“版本 N”、进度记录里的“v3 · checkpoint”（原始动作名），改为中文动作名，去掉版本号。

### 4.4 开发任务文案对照

| 位置 | 现在 | 改为 |
|---|---|---|
| 页头 | 开发任务 / WORKFLOW / RESUMABLE PROGRESS | 任务进度 |
| 页底帮助 | 这里只保存进度，不自动执行代码。完成步骤须关联本轮成功操作并核对结果；取消任务不停止本机命令。 | ChatGPT 记录的分步进度。 |
| 分页 | 每页最多 20 项 | 删除 |
| 新建（交接说明） | 正在按当前角色、Profile 与项目权限核对连接… / 已核对当前可读写的连接；建立任务时会再次检查权限，不会自动执行。 | 删除 |
| 新建（模板说明） | 清单不自动执行命令。 | 删除 |
| 新建（凭据提醒） | 不要在目标和摘要中填写密码、令牌或其他凭据。 | 保留，改为输入框的 placeholder |
| 保存未确认 | 保存结果待核实；再次点击仅重试同一请求。关闭后请先刷新任务列表核对，勿重复新建。 | 网络中断，不确定是否已保存。刷新列表看一下。 |
| 记录进度说明 | 完成步骤必须提供同项目、同授权、本任务建立后的成功操作。命令成功不等于业务验收通过。 | 选这一步用到的操作。 |
| 记录进度底部 | 基于版本 N 保存；取消任务不会中止本机命令。跳过步骤必须说明原因。 | 跳过步骤请写明原因。 |
| 映射变化 | 项目目录或设备已更改。此任务只能核对历史进度，请为新映射建立任务。 | 项目位置已改，这个任务只能查看。 |
| 操作详情标题 | 操作证据 · {tool} | 操作详情 |

---

## 5. 四个页面的共同问题

### 5.1 “重试”有六种说法

| 现在 | 位置 |
|---|---|
| 重试原请求 | CLI 会话 |
| 重试原回答 | CLI 会话审批 |
| 核对原操作 | 协作中心 |
| 核对原消息 / 核对原上传 | 协作中心 |
| 恢复本次检查 | VPS |
| 重新提交原请求 | 面板更新 |

这些按钮背后都是同一个机制：沿用原幂等键重试，不会重复执行。统一叫“重试”。如果担心用户以为会重复执行，就在按钮的 `title` 里写“不会重复执行”，不要写进按钮文字。

### 5.2 界面上的短 ID

以下位置把 `#` 加 8 到 12 位 ID 直接展示给用户：协作中心的成员下拉框、成员详情标题、工作详情（成员、步骤、原操作）、投递详情、房间要点的来源按钮；开发任务的证据按钮；VPS 的检查状态（显示完整操作 ID）。

规则：ID 只在“诊断”或“详情 → 复制 ID”里出现。正常界面用名称和时间区分；同名时才加短 ID。

### 5.3 “核对”

`collaboration-room.js` 里有 61 处。按语境改写：

- 读取最新状态 →“刷新”
- 确认操作是否成功 →“查看结果”
- 要求用户检查 → 直接说要检查什么，例如“看一下权限是否正确”
- 解释系统在做什么 → 删除

第一份文档 5.5 节的检查脚本已经把“核对”列为内部术语规则，可以直接覆盖这四个文件。

---

## 6. 实施顺序

| 阶段 | 内容 | 涉及文件 | 需要补的测试 |
|---|---|---|---|
| P0 | 1.3 建房时选项目、添加 dot 不再被拦；1.4 权限每行一个下拉框；2.1 新建会话时选 CLI；2.4 停止并删除；3.1 新 VPS 默认信任首次连接；3.2 保存并检查 | `collaboration-room.js`、`hub/collaboration/rooms.py`（若采用后端方案）、`chat.js`、`chat-history.js`、`vps.js` | 新房间零项目 / 多项目创建后可以直接添加 dot；权限下拉框保存的结构与旧复选框一致（快照对比）；旧房间仍然被引导到迁移；会话在 `orphaned` 状态下停止并删除时会超时；VPS 新建与编辑的默认值 |
| P0（需维护者决定） | 1.5 微信确认合并 | `room_weixin_native.py`、`channel_*`、`collaboration-room.js` | 首次绑定、重新绑定、`different_peer`、配对码过期；要有安全评审结论 |
| P1 | 1.6 主界面（标题、工作横条、去掉窗口提示、投递一行化、旧房间去重）；1.7 状态收敛；2.2、2.3、2.5、2.6；3.3；4.2、4.3 | 同上，加上 `chat-panels.js`、`chat-chrome.js`、`ui.js`、`workflows.js` | 工作横条的实时更新；⌘K 在聊天页与其他页的行为；操作选择器在没有操作时的提示 |
| P2 | 第 1.8、2.7、3.4、4.4 节文案；第 5 节统一用词；4.1 改名 | 四个页面 | 文案快照更新；第一份文档的检查脚本加入这四个文件 |

P0 中只有 1.3（建房同时写权限）和 1.5（微信确认）会碰到授权语义。

- 1.3 写入的范围是“只读”，并且只针对用户在建房表单里明确勾选的项目，没有扩权。
- 1.5 减少了确认次数，属于安全取舍，需要维护者拍板后再做。

其余各项都只改显示和交互，不改变后端的授权判断。
