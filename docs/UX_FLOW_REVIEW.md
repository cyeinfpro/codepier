# 面板用户流程体验审查

基于 `main@ba09fd3`（v1.33.2）的 `web/` 源码阅读整理。只看面板，不涉及 MCP 工具协议本身。
重点：首次接入主路径、重复和多余的选项、能合并成一次点击的步骤、需要去掉的“AI 味”文案。

协作中心、CLI 会话、VPS、开发任务四个页面见补充文档 [UX_FLOW_REVIEW_PART2.md](UX_FLOW_REVIEW_PART2.md)。

---

## 0. 结论先行

| # | 问题 | 位置 | 优先级 |
|---|---|---|---|
| 1 | 接入电脑要走两个弹窗；执行命令后弹窗不知道电脑已上线，用户要自己关掉弹窗去列表找，再点“映射项目” | `agent-install.js`、`app.js connectEvents` | P0 |
| 2 | 添加项目时目录没授权，会再展开一个“目录授权”子表单，要求再点一次“验证并授权” | `app.js projectModal`、`agent-roots.js` | P0 |
| 3 | 执行权限分三层（Agent 本机 / 项目 / 客户端），默认值互相矛盾：装 Agent 默认开执行，加项目默认不开 | `agent-install.js`、`projectModal` | P0 |
| 4 | ChatGPT 授权页：期限、Profile、四个权限复选框、项目列表全部平铺，写入和执行每次都要手动勾 | `app.js consentModal` | P0 |
| 5 | 空工作区的总览页显示一排 0，没告诉新用户下一步做什么 | `overviewHTML` | P0 |
| 6 | 侧栏 20 个入口，权限相关分散在 7 个页面；每项带无意义编号 `01`–`20`；侧栏搜索和 ⌘K 功能重复 | `app.js nav / renderShell` | P1 |
| 7 | “MCP 接入”页同时放了 3 个复制地址、凭据列表、Profile、自检表单、Tunnel 预览、工具目录 | `connectHTML`、`connection-setup.js` | P1 |
| 8 | 卡片按钮过多：设备卡 5 个按钮加 2 个折叠，项目卡 8 个，授权行 4 个 | `devicesHTML`、`projectsHTML`、`grantListHTML` | P1 |
| 9 | 界面文案中约 250 处“不会 / 不能 / 不是 / 不等于 / 不证明”式免责句，大量“证据、回执、核对、验收、原操作”等内部术语外露 | 全部 `web/*.js` | P1 |

---

## 1. 首次接入主路径

README 写的是 4 步。从代码看，用户实际要做的操作如下（按读代码估算，未做实测录屏）：

| 阶段 | 现在 | 建议后 |
|---|---|---|
| 接入电脑 | 点“接入设备” → 填名称 → 选系统 → 填目录 → 点“生成安装命令” → **新弹窗** → 点“复制” → 终端执行 → 点“完成” → 回列表等上线 | 点“接入电脑” → 填目录（系统已预选）→ 点“生成命令”（自动复制）→ 终端执行 → 弹窗自动变成“已连接” |
| 添加项目 | 在设备卡点“映射项目” → 填别名 → 再填一遍路径 → 选文件权限 → 勾执行 → 保存 → 若目录未授权，展开子表单 → 点“验证并授权” → 等自动续存 | 点“添加项目”（路径已预填）→ 补全子目录（别名自动生成）→ 保存 → 若目录未授权，弹一次确认“授权并保存” |
| 连接 ChatGPT | 去“MCP 接入” → 在 3 个地址里找对的那个复制 → ChatGPT 跳回授权页 → 选期限 → 选 Profile → 勾写入 → 勾执行 → 选项目 → 允许 | 总览第 3 步直接给地址和复制按钮 → ChatGPT 跳回授权页 → 点“允许读写和运行” |
| 合计 | 约 9 个输入、13 次点击、3 次换页 | 约 2 个输入、6 次点击、0 次换页 |

### 1.1 接入电脑：一个弹窗走完，并能感知上线

现状问题：

- `agentInstallSetup` 填完表单后，`showAgentInstallCommand` 又开一个新弹窗。
- 系统必须手选，默认是空的。打开面板的这台浏览器，多数情况就是要接入的电脑，可以直接预选。
- “面板地址”默认就是 `location.origin`，绝大多数人不需要改，却放在第一位。
- 设备名称必填。上线前不知道主机名，用户只能随手写一个。
- `connectEvents` 里 `refreshPanel` 在有弹窗时直接 return（`if (... || $('.modal')) return;`），所以安装弹窗永远收不到“已上线”。
- 命令弹窗有 4 个按钮：完成 / 下载配对文件 / 重新生成 / 复制。“重新生成”只在过期时才有用，可以自动处理；“下载配对文件”是失败后的兜底，不该常驻。
- 首次接入的说明里写着“Windows 失败安装恢复 / 不要直接删除 config.json”。这是故障场景的说明，放进了正常流程。

**建议代码 A：把事件流转发给弹窗**（`web/app.js`，`connectEvents` 的 `onmessage` 内）

```js
    if (['device', 'project'].includes(m.type)) invalidateBasics();
    // 页面刷新在有弹窗时会跳过；弹窗需要自己订阅事件
    window.dispatchEvent(new CustomEvent('cp:event', { detail: m }));
```

**建议代码 B：精简安装表单**（`web/agent-install.js`）

```js
function agentGuessPlatform() {
  const p = String(navigator.userAgentData?.platform || navigator.platform || '').toLowerCase();
  return p.startsWith('win') ? 'windows' : 'posix';
}

function agentInstallFields(platform = '', repair = false) {
  const chosen = platform || agentGuessPlatform();
  const radio = (value, label) =>
    `<label><input type="radio" name="platform" value="${value}" ${chosen === value ? 'checked' : ''}>${label}</label>`;
  return `<div class="field"><label>系统</label><div class="segmented" role="radiogroup">${radio('posix', 'macOS / Linux')}${radio('windows', 'Windows')}</div></div>
    <div class="field"><label>项目所在目录</label><input name="allow_root" required maxlength="2048" autocomplete="off" spellcheck="false"><small>AI 只能读写这个目录里的文件。</small></div>
    <details class="form-advanced"><summary>更多选项</summary>
      ${repair ? '' : '<div class="field"><label>名称</label><input name="name" maxlength="80" placeholder="留空则用电脑主机名"></div>'}
      <div class="field"><label>面板地址</label><input name="hub_url" value="${esc(location.origin)}" required><small>这台电脑要能访问到这个地址。</small></div>
      ${repair ? '' : '<label class="check"><input name="enable_execution" type="checkbox" checked>允许运行命令</label>'}
    </details>`;
}
```

改成单选按钮后，`form.elements.platform` 变成 `RadioNodeList`，没有 `onchange`。`agentInstallSetup` 里的 `form.elements.platform.onchange = hint` 改为：

```js
form.addEventListener('change', (event) => {
  if (event.target.name === 'platform') hint();
});
```

名称留空时传一个临时名，上线后再用主机名改掉：

```js
const typedName = form.elements.name?.value.trim() || '';
options.autoName = !typedName;
pairing = (await post('/api/devices', {
  name: typedName || '新电脑',
  hub_url: options.hub_url,
})).pairing;
```

删掉 `agentInstallSetup` 里首次接入的那段长说明（“仅用于首次接入新节点……不要直接删除 config.json”）。修复模式保留一句即可：“在原电脑上运行，配置和目录授权会保留。”

**建议代码 C：命令弹窗等待上线并引导下一步**（`web/agent-install.js`，`showAgentInstallCommand`）

按钮只留两个：

```js
`<button class="btn ghost" data-action="close-modal">关闭</button><button class="btn primary" id="agent-install-copy" disabled>复制命令</button>`
```

过期后自动换新命令，不再让用户点“重新生成”：

```js
function expiry() {
  if (!current() || !command || Date.now() / 1000 < expires) return;
  command = '';
  field.value = '';
  generate(); // 旧票据已过期，直接换新
}
```

生成成功后尝试自动复制。这次点击就是用户手势，但有些浏览器在 `await` 之后会丢失手势，所以失败时不提示：

```js
// generate() 成功分支末尾
navigator.clipboard?.writeText(command).then(
  () => current() && (status.textContent = '命令已复制。在这台电脑的终端里粘贴运行，' + timeText(expires) + ' 前有效。'),
  () => {},
);
```

监听上线：

```js
const onEvent = async (event) => {
  const m = event.detail;
  if (!current() || m?.type !== 'device' || m.data?.id !== device.id) return;
  await loadBasics().catch(() => {});
  const fresh = S.devices.find((d) => d.id === device.id);
  if (!current() || !fresh?.online) return;
  if (options.autoName && fresh.info?.hostname)
    api('/api/devices/' + encodeURIComponent(device.id), {
      method: 'PATCH',
      body: JSON.stringify({ name: fresh.info.hostname }),
    }).catch(() => {});
  clearInterval(ticker);
  field.hidden = true;
  status.textContent = (fresh.info?.hostname || device.name) + ' 已连接。';
  copyButton.textContent = '添加项目';
  copyButton.disabled = false;
  copyButton.onclick = () => {
    closeModal(dialog);
    projectModal(null, device.id, { root: options.allow_root });
  };
};
window.addEventListener('cp:event', onEvent);
// 事件通道断开时的兜底
const poll = setInterval(() => onEvent({ detail: { type: 'device', data: { id: device.id } } }), 5000);

S.modalCleanup = () => {
  window.removeEventListener('cp:event', onEvent);
  clearInterval(poll);
  // ……原有清理
};
```

“下载配对文件”只在生成失败时出现在状态文字里（现在的 `generate()` catch 分支已经这样写了提示），按钮栏不再常驻。

### 1.2 添加项目：路径和授权一次完成

现状问题：

- 设备上已经填过一次“允许访问的目录”，加项目时还要再输入完整路径，别名也要手填。
- 文件权限是下拉框，执行权限是复选框，两者有依赖关系（只读时执行无意义），却拆成两个控件。
- 新项目执行默认关闭；而装 Agent 时执行默认开启。用户装完以为能跑命令，结果 ChatGPT 报“未授权”。
- 目录不在授权范围时，`projectModal` 会调用 `openRootManager()`，展开一个完整的目录管理组件，用户还要再点一次“验证并授权”。`onApplied` 之后会自动续存，说明这个流程本身可以串起来，只差中间那一次点击。
- 表单顶部的“未单独配置的项目默认使用清洁执行环境……”和用户当前要做的事无关。

**建议代码 D：预填、自动别名、合并权限控件**（`web/app.js`，`projectModal`）

```js
async function projectModal(id = null, device = '', prefill = {}) {
  // ……
  const deviceAllowsTasks = (deviceId) =>
    (S.devices.find((d) => d.id === deviceId)?.info?.roots || []).some((r) => r && r.allow_tasks);
  const access = p.id
    ? p.mode === 'read' ? 'read' : p.allow_tasks ? 'full' : 'write'
    : deviceAllowsTasks(p.device_id || device) ? 'full' : 'write';
```

表单里用一个下拉框替换原来的 `mode` 下拉框和 `allow_tasks` 复选框：

```html
<div class="field"><label>AI 可以</label><select name="access">
  <option value="read">只看代码</option>
  <option value="write">看和改代码</option>
  <option value="full">看、改代码和运行命令</option>
</select></div>
```

`projectData` 映射回原接口字段，后端不用改：

```js
const projectData = () => {
  const { access, ...rest } = Object.fromEntries(new FormData(form));
  return { ...rest, mode: access === 'read' ? 'read' : 'write', allow_tasks: access === 'full' };
};
```

路径预填，别名跟着路径走，用户手动改过别名后就不再覆盖：

```js
form.elements.root.value ||= prefill.root || '';
let aliasTouched = !!p.alias;
form.elements.alias.addEventListener('input', () => { aliasTouched = true; });
form.elements.root.addEventListener('input', () => {
  if (aliasTouched) return;
  const parts = form.elements.root.value.trim().replace(/[\\/]+$/, '').split(/[\\/]/);
  form.elements.alias.value = (parts.pop() || '').slice(0, 64);
});
```

删除“新增或调整目录授权”按钮和表单顶部关于执行环境的说明。

**建议代码 E：目录未授权时，一次确认后自动授权并保存**

`web/agent-roots.js`，`mount` 里的 `show()`：

```js
      if (!initialized && options.initial) {
        initialized = true;
        edit(options.initial, 'upsert', false);
        if (options.autoSave && value.state === 'applied' && value.config_sha256)
          setTimeout(() => current() && save(), 0); // 等 lock() 把按钮状态算完
      }
```

`web/app.js`，`projectModal`：

```js
  const openRootManager = ({ autoSave = false } = {}) => {
    // ……原逻辑不变，只在 mount 参数里加 autoSave
    rootsManager = CodePierRoots.mount(host, data.device_id, {
      autoSave,
      initial: { path: data.root, writable: data.mode === 'write', allow_tasks: data.allow_tasks },
      // ……
    });
  };

  // submit() 的 catch 分支
  } else if (['ROOT_NOT_ALLOWED', 'LOCAL_READ_ONLY', 'LOCAL_TASKS_DISABLED', 'READ_ONLY'].includes(error.code)) {
    resumeSignature = signature;
    const ok = await uiConfirmAction(
      {
        title: '同时授权这个目录？',
        body: `${data.root}\n这台电脑还没允许 CodePier 访问这里。`,
        confirmLabel: '授权并保存',
      },
      { owner: dialog, current },
    );
    openRootManager({ autoSave: ok }); // 取消时仍展开表单，让用户手动调整
  }
```

授权范围仍然是用户刚输入的那个路径，不会扩到父目录，确认这一步也没有省略。这改动了权限路径，按 `CONTRIBUTING.md` 要补测试：节点离线、预检不通过、保存时 `NETWORK_UNCERTAIN`、用户点取消、授权后续存时表单已被修改。

### 1.3 ChatGPT 授权页：两个按钮二选一

现状问题：

- `consentModal` 把期限、Profile、四个权限复选框、项目列表全部平铺。
- 代码注释写着 `Offering capabilities is not consent: optional scopes start unchecked.`，这个原则没错，但实现方式是让用户逐个勾选。点一个写明权限的按钮，同样是明确同意，而且只需一次点击。
- “客户端 ID / 回调地址 / 资源”三行技术信息排在最前面。
- 底部提示“固定授权按本次同意限制；动态角色授权会跟随……”，只有选了角色的人才需要看。

**建议代码 F**（`web/app.js`，`consentModal`）

```js
const requested = new Set(r.scopes);
const devScopes = ['read', 'write', 'execute'].filter((s) => requested.has(s));
const canDev = devScopes.length > 1;
const allProjects = r.access_defaults?.all_projects === true;

const dialog = modal(
  `${esc(r.client_name)} 想访问你的项目`,
  `<p>期限 30 天 · ${allProjects ? '全部项目' : '选择的项目'}。可以随时在“连接”里撤销。</p>
   <details class="form-advanced"><summary>调整期限、项目或权限</summary>
     <div class="field"><label for="oauth-days">期限</label><select id="oauth-days">…原选项…</select></div>
     ${CodePierProfiles.selectorHTML(profiles)}<div data-profile-permissions></div>
     <dl class="kv"><dt>客户端</dt><dd><code>${esc(r.client_id)}</code></dd><dt>回调地址</dt><dd>${esc(r.redirect_uri)}</dd></dl>
   </details>`,
  `<button class="btn ghost" id="deny-consent">拒绝</button>
   <button class="btn ${canDev ? '' : 'primary'}" id="allow-read">只允许读取</button>
   ${canDev ? '<button class="btn primary" id="allow-dev">允许读写和运行</button>' : ''}`,
  false,
  false,
);

const applyPreset = (list) =>
  $$('.modal input[name="scope"]').forEach((input) => {
    if (!input.disabled) input.checked = list.includes(input.value);
  });

$('#allow-read').onclick = () => { applyPreset(['read']); decide(true); };
if (canDev) $('#allow-dev').onclick = () => { applyPreset(devScopes); decide(true); };
```

桌面控制（`computer`）不放进任何预设，只能在“调整”里单独勾选。如果在“调整”里选了 Profile，就隐藏两个预设按钮，换成一个“允许”，权限以 Profile 为准。

### 1.4 总览页：没有设备时显示接入步骤

`overviewHTML` 对新用户显示“在线节点 0/0、映射项目 0、今日操作 0”。改为：前三步没走完时，显示一个带进度的清单。

**建议代码 G**（`web/app.js`）

```js
function onboardingHTML(o) {
  const steps = [
    ['接入电脑', o.online_devices > 0, '<button class="btn primary small" data-action="add-device">接入</button>'],
    ['添加项目', o.projects.length > 0, '<button class="btn primary small" data-action="add-project">添加</button>'],
    [
      '在 ChatGPT 添加连接',
      o.active_grants > 0,
      `<div class="endpoint"><code>${esc(S.settings.mcp_url)}</code><button class="icon-btn" data-action="copy-endpoint" aria-label="复制地址">${icon('copy')}</button></div>`,
    ],
  ];
  const next = steps.findIndex(([, done]) => !done);
  if (next === -1) return '';
  return `<section class="panel onboarding"><ol>${steps
    .map(([label, done, action], i) =>
      `<li class="${done ? 'done' : i === next ? 'current' : ''}"><strong>${label}</strong>${i === next ? action : ''}</li>`)
    .join('')}</ol></section>`;
}
```

`/api/overview` 目前没有连接数，需要后端补一个 `active_grants` 字段（统计当前空间内未撤销、未过期的连接）。`overviewHTML` 开头插入 `onboardingHTML(o)`，三步都完成后自动消失。

---

## 2. 信息架构

### 2.1 侧栏

现在 20 个入口。权限相关的分散在“MCP 接入、访问 Profiles、访问角色、MCP 网关、我的账号、空间成员、身份管理”7 个页面。编号 `01`–`20` 不对应任何东西，而且排序是乱的（“CLI 会话”是 `11`，却排在数组第一位）。

建议合并成 13 个入口。被合并的页面 ID 保留（旧链接 `#profiles` 等仍能打开），只是不在侧栏显示，改为父页面顶部的标签页：

| 分组 | 入口 | 合并进来的页面（作为标签页） |
|---|---|---|
| 工作 | 总览、项目、工作台、CLI 会话、开发任务、协作中心、产物交付 | — |
| 资源 | 电脑、VPS | — |
| 连接 | 连接 | 访问 Profiles、MCP 网关 |
| 连接 | 开发工具 | — |
| 管理 | 成员与权限 | 空间成员、访问角色、身份管理 |
| 管理 | 操作审计、运行诊断、设置 | — |
| （侧栏底部头像） | 我的账号 | — |

**建议代码 H**（`web/app.js`）

```js
// 子页面：仍可路由，不进侧栏，在父页面顶部显示为标签
const navTabs = {
  connect: ['connect', 'profiles', 'mcp-gateway'],
  members: ['members', 'roles', 'identity-admin'],
};
const tabParent = Object.fromEntries(
  Object.entries(navTabs).flatMap(([parent, ids]) => ids.map((id) => [id, parent])),
);

function pageTabsHTML(page) {
  const ids = navTabs[tabParent[page]];
  if (!ids) return '';
  return `<nav class="page-tabs" role="tablist">${ids
    .map((id) => {
      const title = nav.find((row) => row[0] === id)[2];
      return `<button role="tab" data-nav="${id}" aria-selected="${id === page}">${title}</button>`;
    })
    .join('')}</nav>`;
}

// renderShell 里的 groups
const groups = [
  ['工作', ['overview', 'projects', 'workbench', 'native', 'workflows', 'collaboration', 'artifacts']],
  ['资源', ['devices', 'vps']],
  ['连接', ['connect', 'integrations']],
  ['管理', ['members', 'audit', 'diagnostics', 'settings']],
];
// 侧栏按钮去掉 <span class="nav-num">；子页面打开时父入口高亮
const active = tabParent[S.page] || S.page;
```

`nav` 里的名称同步改为：“设备节点”→“电脑”，“项目映射”→“项目”，“MCP 接入”→“连接”，“空间成员”→“成员与权限”，“设置中心”→“设置”。`identity` 页从侧栏移到底部头像的点击菜单。

### 2.2 顶栏

| 元素 | 建议 |
|---|---|
| 时钟 `#clock` | 删除。系统时钟已经有，这里每秒刷新还耗资源 |
| 刷新按钮 | 实时通道已连接时隐藏，只在断开时显示 |
| 侧栏“查找页面”搜索框 | 删除，和右上角“快速前往 ⌘K”重复 |
| 桌面控制图标 | 未配置桌面控制的空间不显示 |

### 2.3 登录页

`renderLogin` 里有两个英文 eyebrow（`CodePier / 远程开发`、`CodePier / 登录`）、一句口号、一个轨道动画标签 `MCP · CodePier · AGENT`，以及页脚“自托管 · 由管理员控制”。保留品牌标识、标题“登录”、表单，以及 HTTP 不加密的提醒，其余删除。

---

## 3. 重复和多余的选项

| 位置 | 现状 | 建议 |
|---|---|---|
| MCP 接入页头 | “创建凭据” + “管理访问 Profiles” 两个按钮；Profile 选择器里又有一个“在新标签页管理 Profiles”链接 | Profile 改为标签页后两个入口都删掉 |
| MCP 接入 · “两种连接方向”卡片 | 专门用一段话解释本页和 MCP 网关的区别 | 删除。网关变成同级标签后不需要解释 |
| MCP 接入 · 连接地址 | 主地址、核心地址、角色地址三个复制按钮并列 | 只显示主地址。另外两个放进“其他客户端”折叠区 |
| MCP 接入 · 创建凭据（PAT） | 主按钮 | 降为“其他客户端”折叠区里的次要按钮。ChatGPT 走 OAuth，用不到 PAT |
| ChatGPT 接入自检 | 先选连接、再选项目、可选填 64 位目录 SHA，然后点“读取连接证据” | 进页面自动检查最近一个有效连接。项目选择和 SHA 输入放进折叠区 |
| Secure MCP Tunnel 预览 | 和自检在同一卡片里 | 移到“其他客户端”折叠区 |
| 工具目录 | 页面底部可搜索的工具网格 | 移到“运行诊断”。普通用户不需要逐个看工具名 |
| 授权行按钮 | 查看实际访问结果 / 修改授权期限 / 调整项目范围 / 撤销 | 合并成“编辑”（一个弹窗：期限 + 项目 + 实际访问预览）+“撤销” |
| 授权行元信息 | “固定授权（fixed）”“原同意能力”“原同意 N 个项目” | 去掉英文括号和“原同意”：“权限：读取 · 写入”“3 个项目” |
| 系统设置 · MCP 默认授权 | 3 个复选框 + 2 段说明，其中“同时应用到已有连接”依赖第一个复选框 | 第一期：删除“应用到已有连接”复选框，改为保存后的 toast 操作“同时应用到 N 个现有连接”。第二期：授权页记住上次选择，整块设置删除 |
| 设备卡 | 更新 / 重启 / 授权目录 / 完整管理 / 映射项目，外加“目录与权限”“完整本机信息”两个折叠区 | 主按钮“添加项目”+“管理”。有新版本时在卡片上显示一个“更新”按钮。JSON 原始信息只放在管理弹窗里 |
| 项目卡 | Pi / Codex / Claude / 协作 / 工作台 / 执行环境 / VPS 分配 / 编辑，共 8 个 | 主按钮“打开”（进工作台），三个 CLI 按钮合成一个“对话”（见补充文档 2.1），其余放进 `uiMenu('更多', …)`（`ui.js` 已有此组件） |
| 项目卡 · “PROJECT”标签 | 每张卡都有英文大写标签 | 删除 |
| 面板更新 | 检查更新 / 刷新状态 / 重新提交原请求 / 刷新到新版本 | 打开即自动检查，删掉“刷新状态”。另外两个改名为“重试”和“重新加载页面” |
| 总览 | 页面标题“控制总览”+副标题“CodePier 工作区”+卡片标题“工作区概览”+标签“CodePier” | 只保留页面标题“总览” |
| 每个页面底部的 `uiHelp` 折叠说明 | 盾牌图标 + 一段说明，大部分是免责内容 | 内容能删的删。保留的并入相关表单字段的 `<small>` |

---

## 4. 安装命令弹窗的文案对比（示例）

**现在**

> 在要接入的电脑上打开终端，粘贴并执行下方命令。这条命令只绑定当前面板节点。新节点可首次安装；已有受管节点必须从原节点生成命令才能修复或升级，错节点会被安全拒绝；失败安装可先执行带备份的 Windows 归档恢复。
>
> 命令会下载并校验当前面板的 Agent 包，使用一次性连接凭据。已有节点的配置、授权目录、状态和日志会保留；请只在对应目标电脑执行。遇到设备身份不一致，请回设备列表找到原节点并重新生成命令；不要删除 config.json。

**建议**

> 在这台电脑的终端里粘贴运行。命令只能用一次，21:40 前有效。

“设备身份不一致”“失败安装恢复”等内容，等真的出错时再由错误信息给出。

---

## 5. 去掉“AI 味”的文案

### 5.1 常见问题

1. **免责声明叠在一起**：“不调用模型、不代你创建授权，也不会运行测试命令”。用户没问的事情不用逐条否认。
2. **内部术语外露**：证据、回执、原操作、核对、核实、验收、目录 SHA、工具合约、有效工具目录。
3. **中文后面括号注英文**：“固定授权（fixed）”“动态角色（role）”。
4. **装饰用的英文大写**：`CONNECTION / ACCESS`、`STREAMABLE HTTP`、`PROJECT`、`SETTINGS`。
5. **空泛口号**：“AI 与本地代码对接、任务停靠的地方”“连接你的开发现场”“按你要做的事开始。”
6. **每个控件下面都配一段说明**（`form-note` 在 `app.js` 中有 13 处）。
7. **“请……，不要……”成对出现**：“请重新读取设置和已有连接核实，不要假定保存失败。”

### 5.2 写法规则

- 说明最多一句。只有当用户需要做决定、且后果不明显时才写。
- 错误信息写两件事：发生了什么，下一步做什么。
- 不写用户没问的事情“不会发生”。
- 同一个东西全站只用一个名字（见 5.4）。
- 不用英文 eyebrow，不在中文后面加英文括号。

### 5.3 逐条改写

| 位置 | 现在 | 改为 |
|---|---|---|
| `connection-setup.js` 标题 | ChatGPT 接入与分层自检 | 连接检查 |
| 同上 · 步骤 1 | ChatGPT 登录、CodePier OAuth/PAT 授权和模型 API 凭据互相独立。MCP 工具本身不要求模型 Key。先核对本页 Hub 地址、当前 Space 和连接所有者。 | 删除整步。需要时只保留一句：“不需要模型 API Key。” |
| 同上 · 步骤 3 | 在客户端刷新工具元数据……这里的检查不调用模型、不代你创建授权，也不会运行测试命令。 | 在 ChatGPT 新建对话，开启 CodePier，让它列出你的项目。 |
| 同上 · 按钮 | 读取连接证据 | 测试连接 |
| 同上 · 进行中 | 正在读取当前授权与历史证据… | 正在测试… |
| 同上 · 状态 `observed` | 已有服务端证据 | 已连通 |
| 同上 · 状态 `stale` | 历史证据，需重新验收 | 需要重新测试 |
| 同上 · 结果尾注 | 记录按原连接、项目与权限范围隔离。旧授权或旧映射证据不能证明现在可用。 | 删除 |
| 同上 · SHA 输入 | 客户端主动报告的目录 SHA（可选）／仅用于比对，不证明 ChatGPT 缓存已刷新。 | 移入折叠区，标签改为“工具目录版本（排查用）” |
| `connectHTML` 副标题 | 助手访问 CodePier · 客户端、Profile 与授权政策 | 删除 |
| `connectHTML` 核心地址说明 | 统一九工具入口；实际权限由当前角色决定。 | 删除（移入折叠区后不需要） |
| `access.js` 说明 1 | 默认值只预选新授权页面，应用接入仍需你确认，且不会超出客户端申请的权限；桌面控制不默认勾选。批量应用仅扩展…也不影响 PAT。 | 新连接的授权页会预先选中这些选项。 |
| `access.js` 说明 2 | 关闭默认选项不会自动撤销已有授权。可在“MCP 接入 → 调整项目范围”逐项收回… | 删除 |
| `access.js` 网络错误 | 保存回执暂未收到。请重新读取设置和已有连接核实，不要假定保存失败。 | 网络中断，不确定是否已保存。刷新页面看一下。 |
| `consentModal` 底部提示 | 固定授权按本次同意限制；动态角色授权会跟随之后的角色能力和项目变更。请核对身份、角色、应用及回调地址。 | 删除。只在选了角色时显示：“权限会随角色变化。” |
| `consentModal` 期限说明 | 无限期允许持续访问，泄露风险持续存在；可随时在 MCP 接入撤销。 | 删除（摘要行已经写了“可以随时撤销”） |
| `newGrant` 提示 | 凭据只显示一次，不会写入浏览器持久存储。适用于 stdio bridge、MCP Inspector 或支持 Bearer 的客户端；ChatGPT 直连使用 OAuth。 | 用于 Claude Code、Inspector 等支持 Bearer 的客户端。只显示这一次。 |
| `newGrant` 结果 | 复制后存入云端受权限保护的 token 文件。任何持有此凭据的人，都能执行已授权操作。 | 拿到这个令牌的人可以用你的权限操作，请妥善保存。 |
| `agentInstallFields` 目录说明 | 填写目标电脑上的完整目录路径。文件工具限制在授权目录内，完整 Shell 不受目录沙箱限制。 | AI 只能读写这个目录里的文件。 |
| `agentInstallFields` 执行说明 | Shell 使用 Agent 当前系统用户的完整权限，不是目录沙箱；还需要面板项目与客户端的执行授权。桌面控制仍独立关闭。取消勾选可安装为无执行能力模式。 | 运行命令时使用这台电脑当前用户的权限，不限于上面的目录。 |
| `projectModal` 顶部 | 未单独配置的项目默认使用清洁执行环境。保存映射后，可从项目卡片的“执行环境”选择其他模式。 | 删除 |
| `projectModal` 路径说明 | 保存时验证目录与权限。SMB 共享请填写节点上已挂载的路径。 | 网络共享请填挂载后的路径。 |
| `projectModal` 执行说明 | 执行还需要本机允许。若本机开启完整 Shell 权限，MCP 可执行任意命令，使用 Agent 系统用户的权限。 | 删除（合并到“AI 可以”下拉框的第三个选项） |
| `projectModal` 删除确认 | 只删除映射，不会删除家里文件。已开始的操作不会自动撤销。继续？ | 电脑上的文件不受影响。 |
| `projectModal` 续存提示 | 原映射已保存。当前新增修改已保留，可继续验证并保存。 | 已保存。你之后的修改还没保存。 |
| `projectsHTML` 帮助 | 通过别名访问项目，例如 ProjectAlpha；不区分大小写。选择“全部现有及未来新增项目”的连接会自动包含新增映射…… | 在 ChatGPT 里用项目名称称呼它，不区分大小写。 |
| `devicesHTML` 帮助 | 节点主动连接 Hub，无需开放家里入站端口。……卸载 Agent 与移除面板节点是两个独立操作，都不会删除项目文件。 | 删除。卸载的说明放到卸载确认框里。 |
| `pairingModal` | 这是设备连接凭据，不是 MCP 访问令牌。请保存配对文件；页面关闭后不能再次查看。 | 关闭后无法再次查看，请先下载。 |
| `deviceDetail` 生命周期说明 | 更新先校验完整新运行时，面板记录结果后才切换；节点有任务执行时不会强行更新或重启。 | 有任务在运行时会等它结束再更新。 |
| `overviewHTML` 帮助 | 按 {时区} 统计今日操作。结果以 Agent 回传为准；调用来源按凭据记录。 | 删除。时区写进“今日操作”卡片的 `title` 属性 |
| `renderLogin` | 连接你的开发现场 / AI 与本地代码对接、任务停靠的地方。 | 删除 |
| `settings.js` 副标题 | 按你要做的事开始。 | 删除 |
| `index.source.html` boot | 正在连接… | 保留（这句没问题） |

### 5.4 统一用词

| 现在混用 | 统一为 | 说明 |
|---|---|---|
| 设备 / 节点 / 电脑 / 本机 / 家里电脑 / Agent | **电脑** | “Agent”只出现在版本号和安装相关的地方 |
| 项目映射 / 映射 / 项目 | **项目** | |
| 凭据 / 授权 / 访问授权 / 连接 / Grant | **连接**（OAuth 客户端）、**访问令牌**（PAT） | |
| 授权目录 / 允许访问的目录 / 目录授权 | **可访问的目录** | |
| Shell 与目录任务 / 本机任务及已授权的 Shell / 执行 | **运行命令** | |
| 验收 / 核对 / 核实 / 证据 / 回执 | **检查 / 确认 / 结果** | |
| 别名 | **名称** | 用户眼里它就是项目名 |

### 5.5 防止回退的检查脚本

把下面的脚本放到 `scripts/check-ui-copy.mjs`，在 CI 里以“只报告”模式运行。改写完成后再改成超过阈值即失败。

```js
#!/usr/bin/env node
// 扫描 web/ 下面向用户的中文文案，报告容易写出“AI 味”的模式
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const rules = [
  [/(不会|不能|不是|并非|不代表|不等于|不证明)[^。；]{0,30}[，；](也|且|并)?(不会|不能|不是|不)/, '一句里连续否定'],
  [/(证据|回执|原操作|验收|核实|工具合约)/, '内部术语'],
  [/[一-龥]（[A-Za-z][A-Za-z ]*）/, '中文后括号注英文'],
  [/请[^。]{0,30}，不要/, '“请……不要……”'],
  [/['"`][A-Z]{3,}( \/ [A-Z]{3,})+['"`]/, '英文大写装饰'],
  [/(节点|家里电脑|映射)/, '统一用词'],
];

const dir = 'web';
const files = readdirSync(dir).filter((f) => f.endsWith('.js'));
let total = 0;
for (const file of files) {
  readFileSync(join(dir, file), 'utf8')
    .split('\n')
    .forEach((line, i) => {
      if (/^\s*\/\//.test(line)) return; // 跳过注释
      for (const [re, label] of rules) {
        const m = line.match(re);
        if (m) {
          total++;
          console.log(`${file}:${i + 1}  [${label}]  ${m[0].slice(0, 40)}`);
        }
      }
    });
}
console.log(`\n共 ${total} 处`);
const max = Number(process.argv.find((a) => a.startsWith('--max='))?.slice(6) ?? Infinity);
process.exit(total > max ? 1 : 0);
```

“统一用词”这条规则会命中协议字段和旧兼容入口里的字符串，第一轮需要人工逐条确认，不要按全文搜索结果直接替换（`CONTRIBUTING.md` 也提到旧命名兼容入口不能只因搜索命中就删）。

---

## 6. 实施顺序

| 阶段 | 内容 | 涉及文件 | 需要补的测试 |
|---|---|---|---|
| P0 | 1.1 安装弹窗合并和上线感知；1.2 项目预填、权限合并、目录自动授权；1.3 授权页预设按钮；1.4 总览接入清单 | `agent-install.js`、`agent-roots.js`、`app.js`、`hub` 的 overview 接口 | 浏览器测试（Chromium + WebKit）：完整走一遍接入流程；目录授权的离线、预检失败、`NETWORK_UNCERTAIN`、取消确认；授权页客户端只申请 `read` 时不显示开发预设；选 Profile 时预设按钮隐藏 |
| P1 | 2.1 侧栏合并和标签页；2.2 顶栏；第 3 节按钮合并；MCP 接入页折叠 | `app.js`、`connection-setup.js`、`access.js`、`panel-update.js`、对应 CSS | 旧 hash 路由（`#profiles`、`#roles` 等）仍能打开；移动端底部 dock 和“更多”菜单正常 |
| P2 | 第 5 节文案改写；统一用词；接入检查脚本；授权页记住上次选择后删除“MCP 默认授权”设置 | `web/*.js`、`scripts/` | 文案快照测试更新；脚本在 CI 中运行 |

P0 里只有 1.2 的“目录自动授权”涉及权限变更，其余都是前端交互调整，不改变后端授权语义。1.4 需要后端在 overview 接口加一个只读计数字段。
