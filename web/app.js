'use strict';
// No external scripts, telemetry, fonts or client-side token persistence.
const $ = CP.ui.$,
  $$ = CP.ui.$$;
const esc = CP.ui.esc;
const paths = CP.ui.paths;
const icon = CP.ui.icon;
const nav = [
  ['native', 'terminal', 'CLI 会话'],
  ['overview', 'dashboard', '总览'],
  ['devices', 'device', '电脑'],
  ['projects', 'folder', '项目'],
  ['vps', 'cloud', 'VPS'],
  ['workbench', 'code', '工作台'],
  ['workflows', 'history', '任务进度'],
  ['collaboration', 'network', '协作中心'],
  ['audit', 'audit', '操作审计'],
  ['connect', 'plug', '连接'],
  ['profiles', 'shield', '访问配置'],
  ['roles', 'shield', '角色'],
  ['mcp-gateway', 'network', 'MCP 网关'],
  ['diagnostics', 'activity', '运行诊断'],
  ['integrations', 'network', '开发工具'],
  ['artifacts', 'download', '产物交付'],
  ['identity', 'key', '我的账号'],
  ['members', 'shield', '成员'],
  ['identity-admin', 'shield', '身份管理'],
  ['settings', 'settings', '设置'],
];
const navTabs = {
  connect: ['connect', 'profiles', 'mcp-gateway'],
  members: ['members', 'roles', 'identity-admin'],
};
const tabParent = Object.fromEntries(
  Object.entries(navTabs).flatMap(([parent, pages]) => pages.map((page) => [page, parent])),
);
function pageTabsHTML(page) {
  const pages = navTabs[tabParent[page]]?.filter(CodePierIdentity.canNavigate);
  if (!pages || pages.length < 2) return '';
  return `<nav class="page-tabs" aria-label="${tabParent[page] === 'connect' ? '连接' : '成员与权限'}">${pages
    .map(
      (id) =>
        `<button type="button" data-nav="${id}" ${id === page ? 'class="active" aria-current="page"' : ''}>${id === 'connect' ? 'ChatGPT' : nav.find((row) => row[0] === id)[2]}</button>`,
    )
    .join('')}</nav>`;
}
const S = {
  space_id: null,
  identity: null,
  session: null,
  page: 'overview',
  overview: null,
  projects: [],
  devices: [],
  settings: null,
  events: null,
  renderSeq: 0,
  auditMode: 'operations',
  auditOffset: 0,
  auditSource: '',
  auditStatus: '',
  auditQuery: '',
  auditNext: null,
  grantPages: { current: 0, history: 0 },
  grantHistoryOpen: false,
  work: {
    project: '',
    path: '',
    sha: '',
    original: '',
    content: '',
    dirty: false,
    truncated: false,
    treePath: '.',
    treeOffset: 0,
    treeNext: null,
    tree: [],
    task: '',
    operation: null,
    console: '',
    terminalSize: 'compact',
    explorerWidth: 240,
    explorerCollapsed: false,
  },
  poll: null,
  modalCleanup: null,
};
const panelDialogs = CP.ui.createModal({
  state: S,
  labelFields: uiLabelFields,
  trapTab: uiTrapTab,
  focusable: uiFocusable,
});
const ACTIVE_STATES = ['queued', 'running', 'reconnecting', 'cancelling', 'unknown'];
const TERMINAL_STATES = ['succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'];
const stateNames = CP.ui.stateNames;
const badge = CP.ui.badge;
const onlineBadge = CP.ui.onlineBadge;
const timeText = CP.ui.timeText;
const shortTime = CP.ui.shortTime;
const json = CP.ui.json;
function sessionValue(key, value) {
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    /* Private browser storage may be disabled. In-memory task tracking still works. */
  }
  return null;
}
function taskSessionValue(key, value, owner = sessionOwner(S.session)) {
  // Old unbound receipts cannot be safely attributed to the signed-in user.
  sessionValue(key, null);
  if (!owner) return null;
  return sessionValue(key + ':' + encodeURIComponent(owner), value);
}
function uid(...args) {
  return CP.ui.uid(...args);
}
function toast(...args) {
  return CP.ui.toast(...args);
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function networkState(text, online = false) {
  if ($('#event-state')) $('#event-state').textContent = text;
  if ($('#event-dot')) $('#event-dot').className = 'dot ' + (online ? 'online' : 'offline');
  if ($('.transport')) $('.transport').dataset.online = String(!!online);
  if ($('#topbar-refresh')) $('#topbar-refresh').hidden = online;
}
function sessionChanged() {
  return Object.assign(new Error('登录状态已变化，请在当前会话中重试。'), {
    code: 'SESSION_CHANGED',
  });
}
function stopEvents() {
  S.events?.close();
  S.events = null;
  clearTimeout(S.eventTimer);
  S.eventTimer = null;
}
function sessionOwner(session) {
  return session?.user_id
    ? 'id:' + session.user_id + ':space:' + (S.space_id || 'legacy')
    : session?.username
      ? 'name:' + session.username
      : null;
}
function hasUnsavedChanges() {
  return (
    !!S.work.dirty ||
    !!window.CodePierSettings?.dirty() ||
    (typeof ChatUI !== 'undefined' &&
      [...ChatUI.views.values()].some(
        (v) =>
          v.draft ||
          v.files.length ||
          v.pending ||
          v.resumeRequest ||
          v.recoveredQueueDrafts?.length ||
          v.recoveredQueueDraft,
      ))
  );
}
function discardLocalWork(owner = sessionOwner(S.session) || S.suspendedUser) {
  window.CodePierSettings?.reset();
  if (typeof chatDetach === 'function') chatDetach(true);
  resetWork('');
  S.work.operation = null;
  S.work.console = '';
  S.taskSubmission = null;
  taskSessionValue('codepier-operation', null, owner);
  taskSessionValue('codepier-task-submission', null, owner);
}
function clearSpaceSnapshots() {
  clearTimeout(S.activityTimer);
  S.activityRevision = (S.activityRevision || 0) + 1;
  S.activeOps = new Set();
  S.activeOpsSeed = 0;
  window.CodePierProjectRuntime?.reset();
  window.CodePierSettings?.reset();
  invalidateBasics();
  Object.assign(S, {
    overview: null,
    projects: [],
    devices: [],
    settings: null,
    grants: [],
    integrations: null,
    workflow: null,
    delivery: null,
    vps: [],
    vpsQuery: '',
    vpsProject: '',
    auditRows: [],
    auditOffset: 0,
    auditQuery: '',
    auditStatus: '',
    auditSource: '',
    auditNext: null,
    uiProjectQuery: '',
    managementTabs: {},
    managementFilters: {},
    basicsRequest: null,
  });
}
function endSession(discard = false) {
  window.CodePierConnectionSetup?.detach();
  window.CodePierCollaboration?.clear();
  window.CodePierCallLog?.clear();
  window.CodePierPanelUpdate?.detach(true);
  // Expiry suspends in-memory drafts, never persists message text or files.
  // Only the same authenticated account can recover them after signing in.
  const owner = sessionOwner(S.session);
  discard = discard || !owner;
  S.suspendedUser = discard ? null : owner;
  S.suspendedMappings = discard
    ? null
    : new Map(
        (S.projects || []).map((project) => [
          project.id,
          { root: project.root, device_id: project.device_id },
        ]),
      );
  window.CodePierIntegrations?.detach();
  S.integrations = null;
  sessionValue('codepier-integration-receipts', null);
  $('#toasts')?.replaceChildren();

  if (typeof chatDetach === 'function') chatDetach(discard);
  stopEvents();
  clearTimeout(S.poll);
  S.poll = null;
  S.session = null;
  S.renderSeq++;
  clearSpaceSnapshots();
  if (typeof stopComputerApprovals === 'function') stopComputerApprovals();
  S.readGeneration = (S.readGeneration || 0) + 1;
  S.treeGeneration = (S.treeGeneration || 0) + 1;
  if (discard) discardLocalWork(owner);
  window.CodePierGateway?.reset();
  window.CodePierIdentity?.reset();
  S.identity = null;
  S.space_id = null;
  renderLogin();
}
async function api(path, options = {}) {
  const {
    retrySafe = false,
    discardOnAuthFailure = false,
    retryDelays = [500, 1000, 2000, 4000],
    requestTimeout = 15000,
    signal: externalSignal,
    ...request
  } = options;
  const session = S.session,
    space = S.space_id,
    method = (request.method || 'GET').toUpperCase();
  const safe = method === 'GET' || retrySafe;
  const headers = {
    ...(space ? { 'X-CodePier-Space': space } : {}),
    ...(request.body ? { 'Content-Type': 'application/json' } : {}),
    ...(session?.csrf ? { 'X-RD-CSRF': session.csrf } : {}),
    ...request.headers,
  };
  const delays = retryDelays;
  for (let attempt = 0; ; attempt++) {
    if (session !== S.session || space !== S.space_id) throw sessionChanged();
    let res, body;
    if (externalSignal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    const controller = new AbortController();
    const abort = () => controller.abort();
    externalSignal?.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(() => controller.abort(), requestTimeout);
    try {
      res = await fetch(path, {
        credentials: 'same-origin',
        ...request,
        headers,
        signal: controller.signal,
      });
      try {
        body = await res.json();
      } catch {
        body = null;
      }
    } catch {
      /* A missing response is ambiguous for a submitted mutation. */
    } finally {
      clearTimeout(timeout);
      externalSignal?.removeEventListener('abort', abort);
    }
    if (externalSignal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    if (session !== S.session || space !== S.space_id) throw sessionChanged();
    if (res) {
      if (res.status === 401 && path != '/api/login') endSession(discardOnAuthFailure);
      if (res.ok && body !== null && body !== undefined) {
        if (method !== 'GET' && method !== 'HEAD') invalidateBasics();
        return body;
      }
      if (
        !res.ok &&
        (!safe || ![408, 429, 500, 502, 503, 504].includes(res.status) || attempt >= delays.length)
      ) {
        const error = new Error(
          body?.error?.message ||
            (typeof body?.error === 'string' ? body.error : '') ||
            `请求失败（HTTP ${res.status}）`,
        );
        if (body?.error && typeof body.error === 'object') Object.assign(error, body.error);
        error.status = res.status;
        throw error;
      }
    }
    if (!safe || attempt >= delays.length)
      throw Object.assign(
        new Error(
          '网络暂时不可用；已提交的操作仍保留在云端。恢复连接后按原操作编号/幂等键核实，不要重复创建任务。',
        ),
        { code: 'NETWORK_UNCERTAIN' },
      );
    networkState(`网络波动 · 正在恢复 ${attempt + 1}/${delays.length}`);
    await pause(delays[attempt]);
  }
}
const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) });
const tool = (name, args = {}) => {
  const remote =
    !name.startsWith('operations_') &&
    !name.startsWith('projects_') &&
    ![
      'workflows_get',
      'workflows_list',
      'diagnostics_get',
      'artifacts_get',
      'artifacts_list',
      'activity_list',
      'workflows_handoff',
    ].includes(name);
  const arguments_ = { ...args };
  if (remote && !arguments_.idempotency_key) arguments_.idempotency_key = 'panel-' + uid();
  return api('/api/tools/call', {
    method: 'POST',
    body: JSON.stringify({ tool: name, arguments: arguments_ }),
    retrySafe: true,
  });
};
const workTarget = (project = S.work.project) => ({
  project,
  ...(project === S.work.project && S.work.workspace_id
    ? { workspace_id: S.work.workspace_id }
    : {}),
});
const wtool = (name, args = {}) => tool(name, { ...workTarget(), ...args });
async function settled(promise) {
  const session = S.session;
  const out = await promise;
  if (S.session !== session) throw sessionChanged();
  if (!out?.pending) return out;
  const id = out.operation_id;
  toast('操作已保存，正在等待本机或恢复连接；无需重新提交。');
  // Keep waiting on the receipt, not on the original POST. A failed GET can be
  // retried safely. No editor state is destroyed when connectivity changes.
  const deadline = Date.now() + 31 * 60 * 1000;
  while (Date.now() < deadline && S.session) {
    if (S.session !== session) throw sessionChanged();
    let op;
    try {
      op = await tool('operations_wait', { operation_id: id, wait_seconds: 8 });
    } catch (error) {
      if (!S.session || ['OPERATION_NOT_FOUND', 'SESSION_CHANGED'].includes(error.code))
        throw error;
      networkState('操作 ' + id.slice(0, 8) + ' · 网络恢复后继续');
      await pause(3000);
      continue;
    }
    if (!op.pending) {
      if (op.state === 'succeeded' && op.result?.ok) return { operation_id: id, ...op.result.data };
      const error = new Error(
        (op.result?.error?.message || op.error || stateNames[op.state] || op.state) +
          ' · 操作 ' +
          id,
      );
      error.code = op.result?.error?.code;
      error.operation_id = id;
      throw error;
    }
    networkState((stateNames[op.state] || op.state) + ' · ' + id.slice(0, 8));
    await pause(400);
  }
  throw new Error('操作仍保存在审计中，编号 ' + id + '；未重复提交。');
}
function download(...args) {
  return CP.ui.download(...args);
}
async function copy(text) {
  const session = S.session,
    space = S.space_id,
    page = S.page,
    epoch = S.renderSeq,
    intent = S.modalIntent || 0;
  const current = () =>
    S.session === session &&
    S.space_id === space &&
    S.page === page &&
    S.renderSeq === epoch &&
    (S.modalIntent || 0) === intent;
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const el = document.createElement('textarea');
      el.value = text;
      el.style.cssText = 'position:fixed;opacity:0;';
      document.body.append(el);
      el.select();
      if (!document.execCommand('copy')) throw new Error('copy');
      el.remove();
    }
    if (current()) toast('已复制');
  } catch {
    if (!current()) return;
    modal('复制内容', `<textarea id="copy-text" rows="5" readonly>${esc(text)}</textarea>`);
    $('#copy-text').select();
  }
}
function closeModal(...args) {
  return panelDialogs.closeModal(...args);
}
function modal(...args) {
  return panelDialogs.modal(...args);
}
function buttons(...args) {
  return CP.ui.buttons(...args);
}
function notice(...args) {
  return CP.ui.notice(...args);
}
function empty(...args) {
  return CP.ui.empty(...args);
}
function heading(...args) {
  return CP.ui.heading(...args);
}
const brand = `<div class="brand"><span class="brand-mark" aria-hidden="true">C</span><div><div class="brand-name">CodePier · 码头</div><span class="brand-sub">AI 与本地代码对接</span></div></div>`;
function appearanceControl() {
  const preference = window.CodePierAppearance?.getPreference() || 'auto';
  const options = [
    ['auto', '跟随系统'],
    ['light', '浅色'],
    ['dark', '深色'],
  ]
    .map(
      ([value, label]) =>
        `<option value="${value}"${value === preference ? ' selected' : ''}>${label}</option>`,
    )
    .join('');
  return `<label class="appearance-control"><span>外观</span><select class="appearance-select" data-ui-appearance aria-label="全站外观">${options}</select></label>`;
}
function renderLogin(configured = true) {
  closeModal();
  document.title = '登录 · CodePier';
  $('#app').innerHTML =
    `<main class="login-screen"><section class="login-panel"><div class="login-head">${brand}${appearanceControl()}</div><h1>登录工作区</h1><div id="oidc-login-buttons" aria-label="外部身份登录"></div>${!configured ? notice('请先在服务器运行 <code>python -m hub init</code> 初始化账号。') : ''}<form id="login-form"><div class="field"><label for="username">账号</label><input id="username" name="username" autocomplete="username" placeholder="输入账号" required maxlength="80"></div><div class="field"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="256" placeholder="输入密码"></div><p id="login-error" class="error-text" role="alert"></p><button class="btn primary" type="submit">登录 ${icon('arrow')}</button></form><div class="spacer"></div>${location.protocol === 'http:' ? notice('HTTP 连接未加密，请在可信网络或 SSH 转发中使用。') : ''}</section></main>`;
  uiLabelFields($('#login-form'));
  CodePierIdentity.loginButtons();
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const b = $('button[type=submit]', e.target);
    b.disabled = true;
    $('#login-error').textContent = '';
    try {
      const r = await post('/api/login', {
        username: $('#username').value,
        password: $('#password').value,
      });
      S.session = r;
      await bootAuthenticated();
    } catch (err) {
      if ($('#login-error')) $('#login-error').textContent = err.message;
      else toast(err.message, true);
    } finally {
      b.disabled = false;
    }
  });
}
function renderShell() {
  const chosen = nav.find((x) => x[0] === S.page);
  const groups = [
    [
      '工作',
      ['overview', 'projects', 'workbench', 'native', 'workflows', 'collaboration', 'artifacts'],
    ],
    ['资源', ['devices', 'vps']],
    ['连接', ['connect', 'integrations']],
    ['管理', ['members', 'audit', 'diagnostics', 'settings']],
  ];
  const navigation = groups
    .map(([label, ids]) => {
      const items = ids
        .map((id) => {
          const page = (navTabs[id] || [id]).find(CodePierIdentity.canNavigate);
          if (!page) return '';
          const [, ico, title] = nav.find((row) => row[0] === id);
          return `<button type="button" data-nav="${page}" data-nav-group="${id}">${icon(ico)}<span>${id === 'members' ? '成员与权限' : title}</span></button>`;
        })
        .join('');
      return items
        ? `<div class="nav-group"><div class="nav-label">${label}</div>${items}</div>`
        : '';
    })
    .join('');
  const dock = [
    ['overview', 'dashboard', '总览'],
    ['projects', 'folder', '项目'],
    ['native', 'terminal', 'CLI'],
    ['workflows', 'history', '任务'],
  ]
    .map(
      ([id, ico, label]) =>
        `<button type="button" data-nav="${id}" aria-label="${label}">${icon(ico)}<span>${label}</span></button>`,
    )
    .join('');
  $('#app').innerHTML =
    `<a class="skip-link" href="#page">跳到主要内容</a><div class="shell"><aside class="sidebar" id="sidebar" aria-label="主导航"><button class="icon-btn mobile-close" data-action="toggle-menu" aria-label="收起菜单">${icon('close')}</button>${brand}<nav class="nav" id="primary-navigation" aria-label="主导航">${navigation}</nav><div class="side-bottom">${CodePierIdentity.selector()}${appearanceControl()}<div class="transport" data-online="false"><div class="transport-top"><i class="dot offline" id="event-dot"></i><span id="event-state">正在连接实时通道…</span></div></div><div class="user-box"><button type="button" class="avatar" data-nav="identity" aria-label="我的账号">${esc(S.session.username?.[0]?.toUpperCase() || 'A')}</button><div>${esc(S.session.username)}<br><small class="tiny">${esc(CodePierIdentity.levelLabel(CodePierIdentity.current()?.level))}</small></div><button class="icon-btn" data-action="logout" aria-label="退出登录">${icon('logout')}</button></div></div></aside><button class="sidebar-scrim" data-ui="close-menu" aria-label="关闭导航" tabindex="-1" hidden></button><main class="main"><header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" data-action="toggle-menu" aria-controls="sidebar" aria-expanded="false" aria-label="展开菜单">${icon('menu')}</button><span class="breadcrumb-prefix">CodePier</span><span>/</span><span id="breadcrumb-group" class="breadcrumb-group" hidden></span><span id="breadcrumb-group-sep" class="breadcrumb-group" hidden>/</span><span id="breadcrumb-page" aria-live="polite">${chosen[2]}</span></div><div class="top-right">${window.CodePierPanelUpdate?.entry() || ''}<button class="quick-jump" data-ui="command" aria-label="快速前往页面或项目">${icon('search')}<span>快速前往</span><kbd>${/Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl'} K</kbd></button><button class="icon-btn" data-computer-use aria-label="桌面控制" hidden>${icon('device')}</button><button class="icon-btn" data-action="refresh" id="topbar-refresh" aria-label="刷新">${icon('refresh')}</button></div></header><div id="page" class="page" tabindex="-1"></div><nav class="mobile-dock" aria-label="移动快捷导航">${dock}<button type="button" data-action="toggle-menu" aria-controls="sidebar" aria-expanded="false" aria-label="更多页面">${icon('menu')}<span>更多</span></button></nav></main></div>`;
  uiSetMenu(false, false);
  uiSyncNavigation();
  CodePierIdentity.bindShell();
  syncComputerShortcut();
}
function computerProjects() {
  return S.projects.filter((p) =>
    S.devices.some((d) => d.id === p.device_id && d.enabled && d.info?.computer_enabled === true),
  );
}
function syncComputerShortcut() {
  const shortcut = $('[data-computer-use]');
  if (!shortcut) return;
  shortcut.hidden = !computerProjects().length;
}
function invalidateBasics() {
  S.basicsRevision = (S.basicsRevision || 0) + 1;
}
async function loadBasics() {
  const session = S.session,
    space = S.space_id,
    revision = S.basicsRevision || 0;
  const pending = S.basicsRequest;
  if (pending?.session === session && pending.space === space && pending.revision === revision)
    return pending.promise;
  const request = { session, space, revision, promise: null };
  request.promise = Promise.all([api('/api/devices'), api('/api/projects')])
    .then(([d, p]) => {
      if (S.session !== session || S.space_id !== space) throw sessionChanged();
      // A completed mutation or mapping event invalidates in-flight snapshots.
      // Join a fresh read rather than publishing data from before that change.
      if ((S.basicsRevision || 0) !== revision) return loadBasics();
      S.devices = d.devices;
      S.projects = p.projects;
      syncComputerShortcut();
    })
    .finally(() => {
      if (S.basicsRequest === request) S.basicsRequest = null;
    });
  S.basicsRequest = request;
  return request.promise;
}
function restoreTaskSubmission() {
  if (S.taskSubmission) return;
  try {
    const r = JSON.parse(taskSessionValue('codepier-task-submission') || 'null');
    if (
      r &&
      typeof r.project === 'string' &&
      typeof r.task === 'string' &&
      typeof r.idempotency_key === 'string'
    )
      S.taskSubmission = r;
  } catch {
    /* Ignore corrupt browser storage. */
  }
}
async function revalidateSuspendedWork() {
  await loadBasics();
  if (!Array.isArray(S.projects)) throw new Error('没有收到有效的项目权限清单，请重新校验。');
  const projects = new Map(S.projects.map((project) => [project.id, project]));
  const sameMapping = (id) => {
    const current = projects.get(id),
      prior = S.suspendedMappings?.get(id);
    return (
      !!current &&
      (!prior || (prior.root === current.root && prior.device_id === current.device_id))
    );
  };
  if (S.work.project && !sameMapping(S.work.project)) {
    resetWork('');
    S.work.operation = null;
    S.work.console = '';
    S.taskSubmission = null;
    taskSessionValue('codepier-operation', null);
    taskSessionValue('codepier-task-submission', null);
  }
  if (typeof ChatUI === 'undefined') return;
  const cachedProjects = [
    ...new Set([...ChatUI.views.keys()].map((key) => key.split('\n')[0]).filter(Boolean)),
  ];
  const allowed = new Set();
  for (const id of cachedProjects) {
    if (!sameMapping(id)) continue;
    try {
      const access = await api('/api/native/access?' + new URLSearchParams({ project: id }));
      if (access.allowed !== true || access.project_id !== id)
        throw new Error('没有收到有效的会话权限确认，请重新校验。');
      allowed.add(id);
    } catch (error) {
      if (![403, 404].includes(error.status)) throw error;
    }
  }
  for (const [key, view] of ChatUI.views) {
    const project = key.split('\n')[0];
    if (!project || allowed.has(project)) continue;
    for (const file of view.files || []) {
      file.removed = true;
      if (file.preview) URL.revokeObjectURL(file.preview);
    }
    Object.assign(view, {
      draft: '',
      files: [],
      pending: null,
      order: [],
      queue: [],
      recoveredQueueDrafts: [],
      recoveredQueueDraft: null,
    });
    view.items?.clear();
    view.outbox?.clear();
    ChatUI.views.delete(key);
  }
  ChatUI.rows = ChatUI.rows.filter((row) => allowed.has(row.project_id || ChatUI.project));
  ChatUI.recentProjects = (ChatUI.recentProjects || []).filter((id) =>
    allowed.has(typeof id === 'string' ? id : id.id),
  );
  ChatUI.catalogCache.clear();
  if (ChatUI.project && !allowed.has(ChatUI.project)) {
    ChatUI.project = '';
    ChatUI.selected = null;
    ChatUI.cwd = '';
    ChatUI.historyProject = '';
  }
}

async function bootAuthenticated() {
  try {
    await CodePierIdentity.bootstrap();
  } catch (error) {
    // Membership discovery must fail closed, while preserving an explicit retry
    // surface. Never start resource streams using a guessed/default Space.
    stopEvents();
    if (typeof stopComputerApprovals === 'function') stopComputerApprovals();
    if (!S.session) return;
    CodePierIdentity.reset();
    S.identity = null;
    S.space_id = null;
    $('#app').innerHTML =
      `<main class="boot"><div id="page" role="alert">${brand}${notice(esc(error.message))}<button class="btn primary" data-action="retry-identity">重试身份与空间校验</button><button class="btn ghost" data-action="logout">退出登录</button></div></main>`;
    return;
  }
  if (S.suspendedUser && S.suspendedUser !== sessionOwner(S.session))
    discardLocalWork(S.suspendedUser);
  if (S.suspendedUser && S.suspendedUser === sessionOwner(S.session)) {
    try {
      await revalidateSuspendedWork();
    } catch (error) {
      if (!S.session) return;
      stopEvents();
      $('#app').innerHTML =
        `<main class="boot"><div id="page" role="alert">${brand}${notice(esc(error.message))}<p>草稿仍隔离在本标签页中；重新确认权限前不会恢复显示或重发请求。</p><button class="btn primary" data-action="retry-identity">重新校验工作区权限</button><button class="btn ghost" data-action="logout">退出登录</button></div></main>`;
      return;
    }
  }
  S.suspendedUser = null;
  S.suspendedMappings = null;
  S.work.operation = S.work.operation || taskSessionValue('codepier-operation');
  restoreTaskSubmission();
  renderShell();
  if (typeof startComputerApprovals === 'function') startComputerApprovals();
  connectEvents();
  await renderPage();
  if (typeof CodePierPanelUpdate !== 'undefined') CodePierPanelUpdate.resume();
  const authId = new URLSearchParams(location.search).get('authorize');
  if (authId && S.session) {
    await loadBasics();
    await consentModal(authId);
  }
}
// Sidebar status dot: 「操作审计」 shows a dot only while operations are
// running. The overview gives a count; live events then track ids.
function navActivitySync() {
  const busy = (S.activeOps?.size || 0) > 0 || (S.activeOpsSeed || 0) > 0;
  $$('.nav [data-nav-group="audit"]').forEach((button) => {
    if (busy) {
      button.dataset.hasActivity = 'true';
      button.title = '有进行中的操作';
    } else {
      delete button.dataset.hasActivity;
      button.removeAttribute('title');
    }
  });
}
const NAV_ACTIVE_STATES = ['queued', 'running', 'reconnecting', 'cancelling'];
function navActivitySeed(count, rows = []) {
  clearTimeout(S.activityTimer);
  S.activityRevision = (S.activityRevision || 0) + 1;
  S.activeOps = new Set(
    rows.filter((row) => NAV_ACTIVE_STATES.includes(row.state)).map((row) => row.id),
  );
  S.activeOpsSeed = Math.max(0, (Number(count) || 0) - S.activeOps.size);
  navActivitySync();
}
function navActivityOperation(data) {
  if (!data?.id) return;
  S.activityRevision = (S.activityRevision || 0) + 1;
  S.activeOps ||= new Set();
  if (NAV_ACTIVE_STATES.includes(data.state)) S.activeOps.add(data.id);
  else S.activeOps.delete(data.id);
  navActivitySync();
  // A count-only snapshot cannot identify which terminal event consumed it.
  // Reconcile unknown ids instead of double-counting or decrementing unrelated events.
  if (S.activeOpsSeed > 0) {
    clearTimeout(S.activityTimer);
    const login = S.session,
      space = S.space_id,
      revision = S.activityRevision;
    S.activityTimer = setTimeout(async () => {
      try {
        const snapshot = await api('/api/overview?include_token_usage=false');
        if (S.session === login && S.space_id === space && S.activityRevision === revision)
          navActivitySeed(snapshot.active_operations, snapshot.recent_operations || []);
      } catch {
        /* Keep the last observed indicator when disconnected. */
      }
    }, 100);
  }
}
function connectEvents() {
  stopEvents();
  const session = S.session,
    events = (S.events = new EventSource(
      '/api/events?' + new URLSearchParams({ space_id: S.space_id || '' }),
    ));
  const current = () => S.events === events && S.session === session;
  let opened = false,
    checkingAccess = false,
    refreshing = false,
    refreshAgain = null;
  const refreshPanel = async (page = S.page) => {
    if (!current() || S.page !== page || $('.modal')) return;
    if (refreshing) {
      refreshAgain = page;
      return;
    }
    refreshing = true;
    try {
      await renderPage(false);
    } catch (error) {
      if (current()) toast(error.message, true);
    } finally {
      refreshing = false;
      if (refreshAgain && current() && refreshAgain === S.page) {
        const pendingPage = refreshAgain;
        refreshAgain = null;
        clearTimeout(S.eventTimer);
        S.eventTimer = setTimeout(() => {
          S.eventTimer = null;
          refreshPanel(pendingPage);
        }, 500);
      }
    }
  };
  events.onopen = () => {
    if (!current()) return;
    networkState('实时通道已连接', true);
    if (typeof computerApprovalsConnection === 'function') computerApprovalsConnection(true);
    if (S.work.operation) pollTask().catch(() => {});
    // Boot already renders the selected page. Only a reconnection needs a
    // second snapshot; otherwise it races and repeats the expensive overview.
    if (!opened) {
      opened = true;
      return;
    }
    if (S.page === 'audit' && S.auditMode === 'operations' && window.CodePierCallLog)
      loadBasics()
        .then(() => current() && CodePierCallLog.refresh(true))
        .catch(() => {});
    else if (['overview', 'devices', 'projects', 'audit', 'workflows', 'vps'].includes(S.page))
      refreshPanel();
    else loadBasics().catch(() => {});
  };
  events.addEventListener('resync', () => {
    if (!current()) return;
    invalidateBasics();
    if (S.work.operation) pollTask().catch(() => {});
    if (typeof refreshComputerApprovals === 'function') refreshComputerApprovals();
    if (typeof CodePierIdentity !== 'undefined')
      CodePierIdentity.refresh().catch((error) => current() && toast(error.message, true));
    loadBasics()
      .then(() => {
        if (!current()) return;
        if (S.page === 'audit' && S.auditMode === 'operations' && window.CodePierCallLog)
          return CodePierCallLog.refresh(true);
        if (['overview', 'devices', 'projects', 'audit', 'workflows', 'vps'].includes(S.page))
          return refreshPanel();
      })
      .catch((error) => current() && toast(error.message, true));
  });
  events.addEventListener('access_revoked', () => {
    if (!current()) return;
    endSession(true);
    toast('当前工作区权限已变化。私有内容已清除，请重新验证身份。', true);
  });
  events.onerror = () => {
    if (current()) {
      networkState('正在重新连接…');
      if (typeof computerApprovalsConnection === 'function') computerApprovalsConnection(false);
      // A reconnect can be rejected before an SSE invalidation frame exists.
      // An ordinary network failure does not erase unsaved local work.
      if (!checkingAccess) {
        checkingAccess = true;
        api('/api/projects', { retryDelays: [], discardOnAuthFailure: true })
          .then(({ projects }) => {
            if (current() && S.projects.some((old) => !projects.some((p) => p.id === old.id)))
              endSession(true);
          })
          .catch((error) => {
            if (
              current() &&
              [
                'SPACE_FORBIDDEN',
                'SPACE_NOT_FOUND',
                'ENTITLEMENTS_STALE',
                'IDENTITY_DISABLED',
                'ACCOUNT_DISABLED',
              ].includes(error.code)
            )
              endSession(true);
          })
          .finally(() => {
            checkingAccess = false;
          });
      }
    }
  };
  events.onmessage = (e) => {
    if (!current()) return;
    let m;
    try {
      m = JSON.parse(e.data);
    } catch {
      return;
    }
    if (m.type === 'operation') navActivityOperation(m.data);
    if (['device', 'project'].includes(m.type)) invalidateBasics();
    if (['device', 'workflow'].includes(m.type))
      window.dispatchEvent(new CustomEvent('cp:event', { detail: m }));
    if (
      ['device', 'project'].includes(m.type) &&
      !['overview', 'devices', 'projects'].includes(S.page)
    )
      loadBasics().catch(() => {});
    if (m.type === 'computer_approval' && typeof refreshComputerApprovals === 'function')
      refreshComputerApprovals();
    if (
      S.work.operation &&
      ['output', 'operation'].includes(m.type) &&
      m.data?.id === S.work.operation
    )
      pollTask().catch(() => {});
    if (
      S.page === 'audit' &&
      S.auditMode === 'operations' &&
      window.CodePierCallLog &&
      ['operation', 'output', 'trace', 'device', 'project'].includes(m.type)
    ) {
      clearTimeout(S.eventTimer);
      S.eventTimer = setTimeout(() => {
        if (current() && S.page === 'audit' && S.auditMode === 'operations')
          CodePierCallLog.refresh(true).catch(() => {});
      }, 500);
      return;
    }
    if (m.type === 'iam') {
      CodePierIdentity.refresh().catch((error) => toast(error.message, true));
    }
    // Refresh only pages whose data changed. Command output/state does not
    // change project/device mappings; rebuilding them on every operation makes
    // ordinary navigation compete with the live audit stream.
    const dependencies = {
      overview: ['operation', 'device', 'project', 'iam'],
      devices: ['device', 'project'],
      projects: ['device', 'project'],
      workflows: ['workflow', 'project', 'device'],
      vps: ['vps', 'project', 'device'],
    };
    if (dependencies[S.page]?.includes(m.type)) {
      const page = S.page;
      clearTimeout(S.eventTimer);
      S.eventTimer = setTimeout(() => {
        S.eventTimer = null;
        if (current() && S.page === page) refreshPanel(page);
      }, 500);
    }
  };
}
async function navigate(page) {
  S.vpsIntent = (S.vpsIntent || 0) + 1;
  if (page === 'terminal') page = 'native';
  if (!S.session || !$('#page') || !nav.some((x) => x[0] === page)) return;
  if (
    page !== S.page &&
    $('.modal') &&
    !(await panelDialogs.requestClose(null, { navigation: true }))
  )
    return;
  if (
    S.page === 'workbench' &&
    S.work.dirty &&
    !(await uiConfirmAction({
      title: '离开当前文件？',
      body: '文件尚未保存，草稿会保留在本标签页中。',
      confirmLabel: '继续离开',
      danger: true,
    }))
  ) {
    history.replaceState(null, '', location.pathname + location.search + '#' + S.page);
    return;
  }
  if (
    S.page === 'settings' &&
    page !== S.page &&
    !(await window.CodePierSettings?.requestLeave())
  ) {
    history.replaceState(null, '', location.pathname + location.search + '#settings');
    return;
  }
  if (page === 'integrations' && S.page !== page) window.CodePierIntegrations?.inherit(S.page);
  const changed = page !== S.page;
  S.page = page;
  location.hash = page;
  const parentGroup = tabParent[page];
  const groupTitle =
    parentGroup === 'connect' ? '连接' : parentGroup === 'members' ? '成员与权限' : '';
  if ($('#breadcrumb-group')) {
    $('#breadcrumb-group').textContent = groupTitle;
    $('#breadcrumb-group').hidden = !groupTitle;
  }
  if ($('#breadcrumb-group-sep')) $('#breadcrumb-group-sep').hidden = !groupTitle;
  $('#breadcrumb-page').textContent = nav.find((x) => x[0] === page)[2];
  uiSetMenu(false, false);
  uiSyncNavigation();
  await renderPage();
  if (changed && S.page === page) {
    window.scrollTo({ top: 0, behavior: 'instant' });
    $('#page h1')?.focus({ preventScroll: true });
  }
}
async function renderPage(showLoading = true) {
  window.CodePierConnectionSetup?.detach();
  window.CodePierSettings?.detach();
  window.CodePierCollaboration?.detach();
  window.CodePierCallLog?.detach();
  window.CodePierPanelUpdate?.detach();
  // Non-loading integration refreshes leave the old tabs visible while basics
  // load. Keep their listeners until bind replaces them so clicks are not lost.
  if (showLoading || !S.session || S.page !== 'integrations' || !$('#integration-center'))
    window.CodePierIntegrations?.detach();
  if (S.page !== 'overview' || !S.session) window.CodePierTokenUsage?.resetDashboard();
  if (!S.session || !$('#page')) return;
  if (S.page === 'native') {
    $('#page').removeAttribute('aria-busy');
    const seq = ++S.renderSeq;
    try {
      await chatPage();
    } catch (e) {
      if (S.session && S.page === 'native' && seq === S.renderSeq) {
        chatDetach();
        $('#page').innerHTML =
          notice(esc(e.message)) + `<button class="btn" data-action="refresh">重试</button>`;
      }
    }
    return;
  }
  if (typeof chatDetach === 'function') chatDetach();
  const seq = ++S.renderSeq;
  const page = S.page;
  const tokenContinuation = page === 'overview' && !!$('#page [data-token-dashboard]');
  if (showLoading) {
    $('#page').setAttribute('aria-busy', 'true');
    $('#page').innerHTML = uiLoadingPage(page);
  }
  try {
    await window.CodePierAssets?.load(page);
    if (seq !== S.renderSeq || !S.session || page !== S.page) return;
    let html = '';
    if (['identity', 'members', 'identity-admin'].includes(page)) {
      html = await CodePierIdentity.html(page);
    } else if (page === 'overview') {
      S.overview = await api('/api/overview?include_token_usage=false');
      if (seq === S.renderSeq && S.session && page === S.page)
        navActivitySeed(S.overview.active_operations, S.overview.recent_operations || []);
      S.overview.token_usage = { loading: true };
      S.projects = S.overview.projects;
      S.devices = S.overview.devices;
      syncComputerShortcut();
      html = overviewHTML();
    } else if (page === 'devices') {
      await loadBasics();
      html = devicesHTML();
    } else if (page === 'projects') {
      await loadBasics();
      html = projectsHTML();
    } else if (page === 'vps') {
      html = await vpsHTML(seq);
    } else if (page === 'workbench') {
      await loadBasics();
      if (!S.work.project && S.projects.length) S.work.project = S.projects[0].id;
      html = workbenchHTML();
    } else if (page === 'collaboration') {
      await loadBasics();
      html = await CodePierCollaboration.html(seq);
    } else if (page === 'workflows') {
      await loadBasics();
      html = await workflowsHTML(seq);
    } else if (page === 'audit') {
      html = await auditHTML(seq);
    } else if (page === 'mcp-gateway') {
      html = await CodePierGateway.html();
    } else if (page === 'roles') {
      await loadBasics();
      html = await CodePierRoles.html();
    } else if (page === 'profiles') {
      await loadBasics();
      html = await CodePierProfiles.html();
    } else if (page === 'connect') {
      S.settings = await api('/api/settings');
      await loadBasics();
      S.grants = (await api('/api/grants')).grants;
      html = connectHTML();
    } else if (page === 'integrations') {
      await loadBasics();
      html = CodePierIntegrations.html();
    } else if (page === 'diagnostics') {
      await loadBasics();
      const [settings, diagnostics] = await Promise.all([
        api('/api/settings'),
        diagnosticsHTML(seq),
      ]);
      html = diagnostics + toolCatalogHTML(settings.tools || []);
    } else if (page === 'artifacts') {
      await loadBasics();
      html = await artifactsHTML(seq);
    } else if (page === 'settings') {
      S.settings = await api('/api/settings');
      html = await settingsHTML();
    }
    html = pageTabsHTML(page) + html;
    if (!showLoading) await uiWaitForPagePointer();
    if (seq !== S.renderSeq || !S.session) return;
    const presentation = showLoading ? null : uiCapturePage();
    if (page === 'vps' && !showLoading) vpsReplacePage(html);
    else if (
      !showLoading &&
      ['overview', 'devices', 'projects', 'audit'].includes(page) &&
      !(page === 'audit' && S.auditMode === 'operations' && window.CodePierCallLog)
    )
      CP.dom.replacePage($('#page'), html);
    else $('#page').innerHTML = html;
    uiPageReady(showLoading, presentation);
    if (['identity', 'members', 'identity-admin'].includes(page)) CodePierIdentity.bind();
    if (page === 'overview')
      void window.CodePierTokenUsage?.loadDashboard(
        $('#page'),
        api,
        tokenContinuation,
        () => seq === S.renderSeq && !!S.session && S.page === 'overview',
      );
    if (page === 'mcp-gateway') CodePierGateway.bind();
    if (page === 'roles') CodePierRoles.bind();
    if (page === 'profiles') CodePierProfiles.bind();
    if (page === 'connect') CodePierConnectionSetup.bind();
    if (page === 'vps') bindVps();
    if (page === 'workbench') bindWorkbench();
    if (page === 'workflows') bindWorkflows();
    if (page === 'collaboration') CodePierCollaboration.bind();
    if (page === 'audit') bindAudit();
    if (page === 'integrations') CodePierIntegrations.bind();
    if (page === 'diagnostics') bindDiagnostics();
    if (page === 'artifacts') bindArtifacts();
    if (page === 'settings') bindSettings();
    CodePierIdentity.applyCapabilities();
  } catch (e) {
    if (seq === S.renderSeq && S.session) uiPageError(e.message, page);
  }
}
function operationsTable(rows) {
  if (!rows.length) return empty('还没有操作记录。');
  return `<div class="table-scroll" role="region" aria-label="操作记录，可滚动查看更多" tabindex="0"><table class="data-table"><thead><tr><th>操作</th><th>项目 / 范围</th><th>调用来源</th><th>时间</th><th>状态</th><th></th></tr></thead><tbody>${rows
    .map((r) => {
      const lifecycle = r.tool?.startsWith('agent_');
      return `<tr data-cp-key="operation:${esc(r.id)}"><td><div class="table-tool">${icon(lifecycle ? 'device' : r.tool?.startsWith('fs_') ? 'file' : r.tool?.startsWith('tasks_') ? 'terminal' : 'code')}<span class="mono">${esc(r.tool)}</span></div></td><td>${esc(r.alias || (lifecycle ? '电脑管理' : '目录验证'))}</td><td><span class="badge ${r.actor?.startsWith('mcp:') ? 'purple' : 'neutral'}">${r.actor?.startsWith('mcp:') ? 'MCP' : '面板'}</span></td><td class="muted mono tiny">${esc(shortTime(r.created))}</td><td>${badge(r.state)}</td><td><button class="icon-btn" data-action="operation-detail" data-id="${esc(r.id)}" aria-label="查看操作详情">${icon('arrow')}</button></td></tr>`;
    })
    .join('')}</tbody></table></div>`;
}
function onboardingHTML(o) {
  const connected = o.online_devices > 0 || o.devices.some((d) => d.last_seen);
  const device = o.devices.find((d) => d.online) || o.devices.find((d) => d.last_seen);
  const more = (list, label) => (list.length > 1 ? ` 等 ${list.length} ${label}` : '');
  // [label, done, action, done summary, preview for steps not reached yet]
  const steps = [
    [
      '接入电脑',
      connected,
      '<button class="btn primary small" data-action="add-device">接入电脑</button>',
      device ? esc(device.name) + more(o.devices, '台') : '已接入',
      '在要使用的电脑上运行一条安装命令。',
    ],
    [
      '添加项目',
      o.projects.length > 0,
      '<button class="btn primary small" data-action="add-project">添加项目</button>',
      o.projects.length ? esc(o.projects[0].alias) + more(o.projects, '个') : '已添加',
      '选择电脑上的一个文件夹，并设置 AI 可以做什么。',
    ],
    [
      '在 ChatGPT 添加连接',
      o.active_grants > 0,
      `<div class="endpoint"><code>${esc(o.mcp_url)}</code><button class="icon-btn" data-action="copy-endpoint" data-url="${esc(o.mcp_url)}" aria-label="复制地址">${icon('copy')}</button></div><small>在 ChatGPT 中添加此地址并确认访问范围。</small>`,
      o.active_grants > 1 ? `${o.active_grants} 个连接` : '已连接',
      '复制 MCP 地址，在 ChatGPT 里添加并确认范围。',
    ],
  ];
  const next = steps.findIndex(([, done]) => !done);
  if (next === -1) return '';
  const doneCount = steps.filter(([, done]) => done).length;
  const progress = `<div class="onboarding-progress"><span>${doneCount} / ${steps.length} 已完成</span><span class="onboarding-progress-bar" role="progressbar" aria-label="设置进度" aria-valuemin="0" aria-valuemax="${steps.length}" aria-valuenow="${doneCount}"><i style="width:${Math.round((doneCount / steps.length) * 100)}%"></i></span></div>`;
  const checklist = `<ol class="onboarding-stepper">${steps
    .map(([label, done, action, summary, preview], index) => {
      const state = done ? 'done' : next === index ? 'current' : 'upcoming';
      const body = done
        ? `<span class="onboarding-step-done">${summary}</span>`
        : next === index
          ? action
          : `<small class="onboarding-step-preview">${esc(preview)}</small>`;
      return `<li class="${done ? 'done' : ''}" data-step-state="${state}" ${next === index ? 'aria-current="step"' : ''}><strong>${esc(label)}</strong>${body}</li>`;
    })
    .join('')}</ol>`;
  if (o.devices.length)
    return `<details id="overview-onboarding" class="panel onboarding onboarding-compact"><summary>继续设置 <strong>${esc(steps[next][0])}</strong><span class="onboarding-summary-count">${doneCount} / ${steps.length}</span></summary>${checklist}</details>`;
  return `<section class="panel onboarding" aria-labelledby="onboarding-title"><div class="onboarding-head"><h2 id="onboarding-title">开始使用</h2>${progress}</div>${checklist}</section>`;
}
function overviewHTML() {
  const o = S.overview;
  const onboarding = onboardingHTML(o);
  if (!o.devices.length && onboarding) return heading('总览') + onboarding;
  const stats = [
    [
      '在线电脑',
      o.online_devices,
      `/ ${o.devices.length}`,
      'device',
      o.online_devices ? '连接已建立' : '等待电脑上线',
      'is-highlight',
    ],
    ['项目', o.projects.length, '', 'folder', '本机项目', ''],
    [
      '今日操作',
      o.today_operations,
      '',
      'activity',
      `成功 ${o.today_succeeded} · 失败 ${o.today_failed}`,
      '',
    ],
    ['执行中', o.active_operations, '', 'terminal', `${o.tool_count} 项 MCP 工具可用`, ''],
  ];
  return (
    heading('总览') +
    onboarding +
    `<div class="overview-grid workspace-overview editorial-board">
      <section class="panel codepier-focus-card overview-summary" aria-label="工作区概览"><div class="stats workspace-stats" aria-label="工作区统计">${stats.map(([label, value, suffix, ico, foot, tone]) => `<article class="stat workspace-stat ${tone}" ${label === '今日操作' ? `title="${esc(o.timezone)}"` : ''}><div class="stat-top"><span>${label}</span>${icon(ico)}</div><div class="stat-value">${esc(value)}<small>${esc(suffix)}</small></div><div class="stat-foot">${esc(foot)}</div></article>`).join('')}</div>${window.CodePierTokenUsage?.dashboard(o.token_usage) || ''}</section>
      <section class="panel workspace-operations"><div class="panel-head"><h2>最近操作</h2><button class="btn ghost small" data-nav="audit">查看全部 ${icon('arrow')}</button></div>${operationsTable(o.recent_operations.slice(0, 6))}</section>
      <section class="panel overview-projects workspace-recent-projects"><div class="panel-head"><h2>项目入口</h2><button class="icon-btn" data-nav="projects" aria-label="查看全部项目">${icon('arrow')}</button></div>${
        o.projects.length
          ? o.projects
              .slice(0, 4)
              .map(
                (p) =>
                  `<div class="project-mini"><span class="project-index" aria-hidden="true">${icon('folder')}<i class="dot ${p.online ? 'online' : 'offline'}"></i></span><div class="project-mini-main"><h3>${esc(p.alias)}</h3><small>${esc(p.device_name)} · ${p.online ? '在线' : '离线'}</small><p title="${esc(p.root)}">${esc(p.root)}</p></div><button class="icon-btn" data-action="open-project" data-id="${esc(p.id)}" aria-label="打开 ${esc(p.alias)}">${icon('arrow')}</button></div>`,
              )
              .join('')
          : empty('添加项目，开始远程开发。', 'add-project', '添加项目')
      }</section>
    </div>`
  );
}
function devicesHTML() {
  const cards = S.devices
    .map((d) => {
      const info = d.info || {};
      const osRaw = String(info.platform || '').toLowerCase();
      const osTag =
        osRaw.includes('darwin') || osRaw.includes('mac')
          ? 'macOS'
          : osRaw.includes('win')
            ? 'Win'
            : osRaw.includes('linux')
              ? 'Linux'
              : '';
      const state = !d.enabled ? 'disabled' : d.online ? 'online' : 'offline';
      return `<article class="panel device-card workspace-device" data-cp-key="device:${esc(d.id)}" data-device-state="${state}">
      <header class="workspace-device-head"><div class="device-symbol workspace-device-symbol">${icon('device')}${osTag ? `<span class="workspace-device-os">${osTag}</span>` : ''}</div><div class="workspace-device-title">${d.enabled ? onlineBadge(d.online) : '<span class="badge failed">已停用</span>'}<h2>${esc(d.name)}</h2></div></header>
      <dl class="card-meta workspace-device-facts"><div><dt>平台 / 主机</dt><dd>${esc(info.platform || '等待上线')} · ${esc(info.hostname || '—')}</dd></div><div><dt>项目</dt><dd>${esc(d.project_count)} 个</dd></div><div><dt>最近连接</dt><dd>${esc(d.last_seen ? shortTime(d.last_seen) : '未连接')}</dd></div><div><dt>可访问的目录</dt><dd>${Array.isArray(info.roots) ? info.roots.length : 0} 个</dd></div></dl>
      <div class="actions workspace-device-actions"><button class="btn primary small" data-action="add-project" data-device="${esc(d.id)}">${icon('plus')}添加项目</button><button class="btn ghost small" data-action="device-detail" data-id="${esc(d.id)}">管理</button>${d.agent?.update_available && d.agent?.can_update && !agentLifecycleBusy(d) ? `<button class="btn small" data-action="agent-update" data-id="${esc(d.id)}">更新</button>` : ''}</div>
      ${agentLifecycleSummary(d)}
    </article>`;
    })
    .join('');
  return (
    heading(
      '电脑',
      '',
      '',
      `<button class="btn primary" data-action="add-device">${icon('plus')}接入电脑</button>`,
    ) +
    `<section class="cards device-cards workspace-device-collection">${cards || empty('还没有电脑。')}</section>`
  );
}
function projectsHTML() {
  const rows = S.projects
    .map(
      (p) =>
        `<article class="panel workspace-project-row" data-cp-key="project:${esc(p.id)}" data-project-row data-project-search="${esc((p.alias + ' ' + p.root + ' ' + p.device_name + ' ' + (p.description || '')).toLocaleLowerCase())}"><header class="workspace-project-head"><span class="project-index" aria-hidden="true">${icon('folder')}</span><div><h2>${esc(p.alias)}</h2>${p.description ? `<p>${esc(p.description)}</p>` : ''}</div>${onlineBadge(p.online)}</header><div class="workspace-project-path"><span>本机路径</span><code title="${esc(p.root)}">${esc(p.root)}</code><button type="button" class="icon-btn workspace-copy-path" data-copy="${esc(p.root)}" aria-label="复制本机路径" title="复制路径">${icon('copy')}</button></div><dl class="workspace-project-facts"><div><dt>设备</dt><dd>${esc(p.device_name)}</dd></div><div><dt>文件权限</dt><dd><span class="badge ${p.mode === 'write' ? 'purple' : 'neutral'}">${p.mode === 'write' ? '可编辑' : '只读'}</span></dd></div><div><dt>任务执行</dt><dd><span class="badge neutral">${p.allow_tasks ? '已授权' : '未授权'}</span></dd></div></dl><footer class="workspace-project-actions"><button class="btn primary small" data-action="open-project" data-id="${esc(p.id)}">打开 ${icon('arrow')}</button>${typeof chatProjectButtons === 'function' ? chatProjectButtons(p.id) : ''}${uiMenu('更多', `<button class="btn small" data-action="project-collaboration" data-id="${esc(p.id)}">协作</button><button class="btn ghost small" data-runtime-project="${esc(p.id)}">执行环境</button><button class="btn ghost small" data-vps-action="project" data-project="${esc(p.id)}">VPS</button><button class="btn ghost small" data-devtools="overview" data-project="${esc(p.id)}">开发工具</button><button class="btn ghost small" data-devtools="validation" data-project="${esc(p.id)}">测试</button><button class="btn ghost small" data-devtools="handoff" data-project="${esc(p.id)}">任务交接</button><button class="btn ghost small" data-action="edit-project" data-id="${esc(p.id)}">编辑</button>`)}</footer></article>`,
    )
    .join('');
  return (
    heading(
      '项目',
      '',
      '在 ChatGPT 里用项目名称称呼它。',
      `<button class="btn primary" data-action="add-project">${icon('plus')}添加项目</button>`,
    ) +
    (S.projects.length
      ? `<div class="project-search workspace-project-search"><label for="project-query">${icon('search')}<input id="project-query" type="search" aria-label="筛选项目" placeholder="搜索项目、电脑或路径"></label><small id="project-count" role="status"></small></div>`
      : '') +
    `${S.projects.length ? `<section class="workspace-project-collection">${rows}<div class="empty panel" id="project-no-match" hidden><p>没有匹配的项目</p><button type="button" class="btn ghost small" data-ui="clear-project-query">清除筛选</button></div></section>` : empty('接入电脑后即可添加项目。', 'add-project', '添加项目')}`
  );
}
async function addDevice() {
  return agentInstallSetup();
}
function pairingModal(pairing, rePair = false) {
  modal(
    rePair ? '更新同一设备的连接凭据' : '设备密钥已生成',
    `${notice('请先下载配对文件，关闭后无法再次查看。')}<div class="spacer"></div><div class="code-box"><pre>${esc(json(pairing))}</pre></div><p class="form-note">${rePair ? '在原 Agent 运行目录使用原配置重新配对；保留目录权限、任务和历史。使用自定义配置时，在 init 前加 --config 指定原文件。完成后按原维护流程重启。' : '在目标电脑运行下方命令，把示例目录换成你的路径。'}</p><div class="code-box"><pre>${rePair ? 'python -m agent init --re-pair --pairing-file pairing.json' : 'python -m agent init --pairing-file pairing.json --allow &quot;D:/Projects&quot;'}</pre></div>`,
    `<button class="btn ghost" data-action="close-modal">已保存</button><button class="btn primary" id="download-pairing">${icon('download')}下载 pairing.json</button>`,
  );
  $('#download-pairing').onclick = () => download('pairing.json', json(pairing));
  loadBasics().catch(() => {});
}
async function deviceDetail(id) {
  const d = S.devices.find((device) => device.id === id);
  if (!d) return;
  const uninstall = d.agent?.can_uninstall
    ? `<button class="btn danger small" data-action="agent-uninstall" data-id="${esc(d.id)}">${icon('trash')}卸载本机 Agent</button>`
    : '';
  const dialog = modal(
    '管理电脑 · ' + d.name,
    `<div class="field"><label>电脑名称</label><input id="device-rename" value="${esc(d.name)}" maxlength="80"></div><dl class="kv device-kv"><dt>电脑编号</dt><dd><code>${esc(d.id)}</code></dd><dt>连接状态</dt><dd>${d.enabled ? onlineBadge(d.online) : '<span class="badge failed">已停用</span>'}</dd><dt>最近连接</dt><dd>${esc(timeText(d.last_seen))}</dd><dt>平台 / 主机</dt><dd>${esc(d.info.platform || '等待上线')} · ${esc(d.info.hostname || '—')}</dd><dt>项目</dt><dd>${d.project_count} 个</dd></dl>${agentLifecycleSummary(d)}<section class="node-manage-section"><div class="node-section-head"><div><h3>Agent 生命周期</h3><p>有任务运行时，更新会等待任务结束。</p></div></div><div class="actions">${agentLifecycleButtons(d, false)}</div></section><section class="node-manage-section"><div id="device-roots-manager"></div></section><section class="node-manage-section"><h3>开发工具配置</h3><p class="form-note">为项目配置语言服务和后台浏览器。</p><div class="actions">${
      S.projects
        .filter((p) => p.device_id === d.id)
        .map(
          (p) =>
            `<button class="btn ghost small" data-devtools="setup" data-project="${esc(p.id)}">${esc(p.alias)} · 配置引导</button>`,
        )
        .join('') ||
      `<button class="btn ghost small" data-action="add-project" data-device="${esc(d.id)}">先添加项目</button>`
    }</div></section><details class="device-raw"><summary>本机声明与能力</summary><div class="code-box"><pre>${esc(json(d.info))}</pre></div></details><p class="form-note">更换 IP / 端口：在目标电脑运行 <code>python -m agent configure --hub http://新IP:新端口</code>。新增或调整授权目录可直接使用上方的目录管理。</p><section class="danger-zone"><div><h3>电脑与凭据</h3><p>卸载只清理目标电脑上的 Agent；“移除面板记录”只删除 Hub 中的电脑身份。两者都不会删除项目文件。</p></div><div class="actions">${uninstall}<button class="btn small" id="toggle-device">${d.enabled ? '停用电脑' : '启用电脑'}</button><button class="btn small" id="rotate-device">轮换连接密钥</button><button class="btn danger small" id="delete-device">仅移除面板记录</button></div></section>`,
    buttons('rename-device', '保存名称'),
    true,
  );
  CodePierRoots.mount($('#device-roots-manager', dialog), d.id);
  $('#rename-device', dialog).onclick = () =>
    busy($('#rename-device', dialog), async () => {
      await api('/api/devices/' + id, {
        method: 'PATCH',
        body: JSON.stringify({ name: $('#device-rename', dialog).value }),
      });
      closeModal(dialog);
      await renderPage();
    });
  $('#toggle-device', dialog).onclick = async () => {
    if (
      d.enabled &&
      !(await uiConfirmAction(
        {
          title: '停用电脑？',
          body: '停用会断开电脑；已经开始的本机操作可能仍会继续。确认停用？',
          confirmLabel: '停用电脑',
          danger: true,
        },
        { owner: dialog },
      ))
    )
      return;
    await busy($('#toggle-device', dialog), async () => {
      await api('/api/devices/' + id, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !d.enabled }),
      });
      closeModal(dialog);
      await renderPage();
    });
  };
  $('#rotate-device', dialog).onclick = async () => {
    if (
      !(await uiConfirmAction(
        {
          title: '轮换连接密钥？',
          body: '旧连接密钥会立刻失效，在线 Agent 将断开。需要在目标电脑更新配置或重新执行安装命令。继续？',
          confirmLabel: '轮换密钥',
          danger: true,
        },
        { owner: dialog },
      ))
    )
      return;
    await busy($('#rotate-device', dialog), async () => {
      const result = await post(`/api/devices/${id}/rotate`);
      if (dialog.isConnected && $('.modal') === dialog) pairingModal(result.pairing, true);
    });
  };
  $('#delete-device', dialog).onclick = async () => {
    if (
      !(await uiConfirmAction(
        {
          title: '移除电脑记录？',
          body: '仅移除面板中的电脑身份？必须先移除它的项目；目标电脑上的 Agent 和项目文件都不会被删除。',
          confirmLabel: '移除记录',
          danger: true,
        },
        { owner: dialog },
      ))
    )
      return;
    await busy($('#delete-device', dialog), async () => {
      await api('/api/devices/' + id, { method: 'DELETE' });
      closeModal(dialog);
      await renderPage();
    });
  };
}
async function projectModal(id = null, device = '', prefill = {}) {
  const session = S.session,
    space = S.space_id,
    page = S.page,
    modalIntent = S.modalIntent || 0,
    ticket = (S.projectModalIntent = (S.projectModalIntent || 0) + 1);
  await loadBasics();
  if (
    S.session !== session ||
    S.space_id !== space ||
    S.page !== page ||
    S.projectModalIntent !== ticket ||
    (S.modalIntent || 0) !== modalIntent
  )
    return;
  if (!S.devices.length) {
    toast('请先接入一台电脑');
    return addDevice();
  }
  const p = S.projects.find((p) => p.id === id) || {};
  const dialog = modal(
    id ? '编辑项目' : '添加项目',
    `<form id="project-form"><div class="field"><label>电脑</label><select name="device_id">${S.devices.map((d) => `<option value="${esc(d.id)}" ${(p.device_id || device) === d.id ? 'selected' : ''}>${esc(d.name)} · ${d.online ? '在线' : '离线'}</option>`).join('')}</select></div>
      <div class="field"><label>项目路径</label><input name="root" required value="${esc(p.root || prefill.root || '')}" placeholder="/Users/me/Projects/ProjectAlpha"><small>网络共享请填挂载后的路径。</small></div>
      <div class="field"><label>项目名称</label><input name="alias" required value="${esc(p.alias || '')}" placeholder="ProjectAlpha" maxlength="64"></div>
      <div class="field"><label>AI 可以</label><select name="access"><option value="read">只看代码</option><option value="write">看和改代码</option><option value="full">看、改代码和运行命令</option></select></div>
      <details class="form-advanced"${p.description ? ' open' : ''}><summary>项目说明</summary><div class="field"><label>说明</label><input name="description" value="${esc(p.description || '')}" maxlength="1000"></div></details>
      <div id="project-roots-manager" hidden></div></form>`,
    `${id ? '<button class="btn danger" id="unmap-project">删除项目</button>' : ''}${buttons('save-project', '保存')}`,
  );
  const form = $('#project-form', dialog),
    button = $('#save-project', dialog),
    status = document.createElement('p');
  status.className = 'form-note';
  status.setAttribute('role', 'status');
  form.append(status);
  const current = () =>
    S.session === session &&
    S.space_id === space &&
    S.page === page &&
    dialog.isConnected &&
    $('.modal') === dialog;
  let submission = null,
    resumeSignature = null,
    rootsManager = null,
    validationTimer = null;
  const root = form.elements.root,
    alias = form.elements.alias,
    access = form.elements.access;
  let aliasTouched = !!p.alias,
    accessTouched = !!p.id;
  function selectedRoot() {
    const info = S.devices.find((d) => d.id === form.elements.device_id.value)?.info;
    const windows = /^win/i.test(info?.platform || '');
    const normalize = (value) => {
      const normalized = windows ? value.replace(/\\/g, '/').toLowerCase() : value;
      return normalized.replace(/\/+$/, '');
    };
    const path = normalize(root.value.trim());
    return (info?.roots || [])
      .filter((r) => r?.path && !r.path.includes('\\..\\'))
      .map((entry) => ({ entry, path: normalize(entry.path) }))
      .filter((r) => path === r.path || path.startsWith(r.path + '/'))
      .sort((a, b) => b.path.length - a.path.length)[0]?.entry;
  }
  function updateDefaults() {
    if (!aliasTouched)
      alias.value = (
        root.value
          .trim()
          .replace(/[\\/]+$/, '')
          .split(/[\\/]/)
          .pop() || ''
      ).slice(0, 64);
    if (!accessTouched) {
      const allowed = selectedRoot();
      access.value = allowed?.writable === false ? 'read' : allowed?.allow_tasks ? 'full' : 'write';
    }
  }
  const prefillDevice = () => {
    const first = S.devices.find((d) => d.id === form.elements.device_id.value)?.info?.roots?.[0];
    root.value ||= (typeof first === 'string' ? first : first?.path) || '';
  };
  if (!p.id) prefillDevice();
  if (p.id) access.value = p.mode === 'read' ? 'read' : p.allow_tasks ? 'full' : 'write';
  updateDefaults();
  alias.addEventListener('input', () => {
    aliasTouched = true;
  });
  access.addEventListener('change', () => {
    accessTouched = true;
  });
  root.addEventListener('input', updateDefaults);
  const projectData = () => {
    const { access, ...rest } = Object.fromEntries(new FormData(form));
    return { ...rest, mode: access === 'read' ? 'read' : 'write', allow_tasks: access === 'full' };
  };
  const openRootManager = (data, signature) => {
    if (!current() || JSON.stringify(projectData()) !== signature) return;
    const host = $('#project-roots-manager', dialog);
    rootsManager?.dispose();
    host.hidden = false;
    rootsManager = CodePierRoots.mount(host, data.device_id, {
      initial: { path: data.root, writable: data.mode === 'write', allow_tasks: data.allow_tasks },
      autoSave: () => current() && JSON.stringify(projectData()) === signature,
      current: () => current() && form.elements.device_id.value === data.device_id,
      onApplied: () => {
        if (!current()) return;
        if (resumeSignature === signature && JSON.stringify(projectData()) === signature) {
          submission = null;
          resumeSignature = null;
          host.hidden = true;
          status.textContent = '目录已授权，正在保存项目…';
          // The original Save handler may still be releasing its busy button.
          validationTimer = setTimeout(() => {
            if (current() && JSON.stringify(projectData()) === signature) submit();
          }, 0);
        } else {
          status.textContent = '目录已授权。请检查当前输入后保存项目。';
        }
      },
    });
  };
  form.elements.device_id.addEventListener('change', () => {
    rootsManager?.dispose();
    rootsManager = null;
    $('#project-roots-manager', dialog).hidden = true;
    resumeSignature = null;
    clearTimeout(validationTimer);
    if (!p.id) {
      root.value = '';
      prefillDevice();
    }
    updateDefaults();
  });
  const previousCleanup = S.modalCleanup;
  S.modalCleanup = () => {
    previousCleanup?.();
    rootsManager?.dispose();
    clearTimeout(validationTimer);
  };
  const submit = () =>
    busy(button, async () => {
      clearTimeout(validationTimer);
      if (!current() || !form.reportValidity()) return;
      const data = projectData();
      const signature = JSON.stringify(data);
      if (submission && submission.signature !== signature && submission.uncertain) {
        status.textContent = '上次保存结果待确认。请恢复原输入后重试，或刷新项目列表。';
        return;
      }
      if (!submission || submission.signature !== signature)
        submission = { signature, args: { ...data, idempotency_key: 'project-save-' + uid() } };
      status.textContent = '正在验证目录并保存…';
      try {
        const saved = await api('/api/projects' + (id ? '/' + id : ''), {
          method: id ? 'PUT' : 'POST',
          body: JSON.stringify(submission.args),
        });
        if (!current()) return;
        if (JSON.stringify(projectData()) !== signature) {
          if (!id && typeof saved?.id === 'string') {
            id = saved.id;
            $('#modal-title', dialog).textContent = '编辑项目';
          }
          submission = null;
          resumeSignature = null;
          status.textContent = '项目已保存。你之后的修改还没保存。';
          return;
        }
        closeModal(dialog);
        toast('项目已保存');
        await renderPage(false);
      } catch (error) {
        submission.uncertain = error.code === 'NETWORK_UNCERTAIN' || error.status >= 500;
        // A definitive rejection did not save a mapping. A new explicit click
        // may validate changed local permissions instead of replaying a failed read.
        if (error.code !== 'VALIDATION_PENDING' && !submission.uncertain) submission = null;
        if (!current()) return;
        if (JSON.stringify(projectData()) !== signature) {
          resumeSignature = null;
          status.textContent = `${error.message} 当前输入已有修改，已保留草稿。`;
          return;
        }
        status.textContent =
          error.code === 'VALIDATION_PENDING' ? '正在等待电脑检查目录…' : error.message;
        if (error.code === 'VALIDATION_PENDING') {
          validationTimer = setTimeout(() => {
            if (current() && JSON.stringify(projectData()) === signature) submit();
          }, 1800);
        } else if (
          ['ROOT_NOT_ALLOWED', 'LOCAL_READ_ONLY', 'LOCAL_TASKS_DISABLED', 'READ_ONLY'].includes(
            error.code,
          )
        ) {
          const accessLabel = data.allow_tasks
            ? '读取、修改和运行命令'
            : data.mode === 'write'
              ? '读取和修改'
              : '读取';
          const approved = await uiConfirmAction(
            {
              title: '授权目录并保存项目？',
              body:
                data.root +
                '\n允许' +
                accessLabel +
                '。' +
                (data.allow_tasks ? '命令使用这台电脑当前用户的权限。' : ''),
              confirmLabel: '授权并保存',
            },
            { owner: dialog, current },
          );
          if (!approved || !current() || JSON.stringify(projectData()) !== signature) return;
          resumeSignature = signature;
          openRootManager(data, signature);
        }
      }
    });
  button.onclick = submit;
  form.onsubmit = (e) => {
    e.preventDefault();
    submit();
  };
  if (id) {
    const remove = $('#unmap-project', dialog);
    remove.onclick = () =>
      busy(remove, async () => {
        if (
          !current() ||
          !(await uiConfirmAction(
            {
              title: '删除项目？',
              body: '只删除项目，不会删除家里文件。已开始的操作不会自动撤销。继续？',
              confirmLabel: '删除项目',
              danger: true,
            },
            { owner: dialog, current },
          ))
        )
          return;
        await api('/api/projects/' + id, { method: 'DELETE' });
        if (S.session !== session) return;
        if (S.work.project === id) resetWork('');
        if (current()) {
          closeModal(dialog);
          await renderPage(false);
        }
      });
  }
}
function resetWork(project, workspace_id = '') {
  S.workGeneration = (S.workGeneration || 0) + 1;
  S.fileGeneration = (S.fileGeneration || 0) + 1;
  S.readGeneration = (S.readGeneration || 0) + 1;
  S.treeGeneration = (S.treeGeneration || 0) + 1;
  Object.assign(S.work, {
    project,
    workspace_id,
    pane: 'files',
    path: '',
    sha: '',
    original: '',
    content: '',
    dirty: false,
    truncated: false,
    tree: [],
    treePath: '.',
    treeOffset: 0,
    treeNext: null,
  });
}
function workbenchHTML() {
  const w = S.work,
    p = S.projects.find((p) => p.id === w.project);
  const more = `<button class="btn ghost" data-devtools="worktrees">${icon('folder')}隔离工作目录</button><button class="btn ghost" data-devtools="handoff">${icon('history')}任务衔接</button><button class="btn ghost" data-insight="symbols">${icon('code')}代码结构</button><button class="btn ghost" data-wf-action="context">${icon('file')}项目上下文</button><button class="btn ghost" data-action="checkpoint">${icon('history')}建立源码检查点</button>`;
  return (
    // One IDE header bar: title, project, status and every workbench tool.
    `<header class="page-head workbench-head"><h1>远程工作台</h1><div class="work-controls ide-toolbar" role="toolbar" aria-label="工作台工具"><select id="work-project" aria-label="选择项目"${p ? ` title="${esc(p.device_name || '')} / ${p.mode === 'write' ? '可编辑' : '只读'}"` : ''}><option value="">选择项目</option>${S.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === w.project ? 'selected' : ''}>${esc(p.alias)} · ${p.online ? '在线' : '离线'}</option>`).join('')}</select>${p ? onlineBadge(p.online) : ''}${w.workspace_id ? `<span class="badge neutral" title="${esc(w.workspace_id)}">隔离目录 ${esc(w.workspace_id.slice(0, 8))}</span><button class="btn ghost small" data-devtools="worktrees">管理工作目录</button>` : ''}<span class="muted tiny work-target">${esc(p?.device_name || '')} ${p ? `/ ${p.mode === 'write' ? '可编辑' : '只读'}` : ''}</span><div class="actions">${S.work.project && !S.work.workspace_id && typeof chatProjectButtons === 'function' ? chatProjectButtons(S.work.project) : ''}<button class="btn ghost small" data-action="git-status">${icon('git')}Git 状态</button><button class="btn ghost small" data-action="git-diff">差异</button><button class="btn ghost small" data-action="history">${icon('history')}备份</button><span class="work-toolbar-sep" aria-hidden="true"></span><button class="btn ghost small" data-devtools="validation" title="测试验收">${icon('check')}验收</button><button class="btn ghost small" data-insight="search" title="项目检索">${icon('search')}检索</button><button class="btn ghost small" data-local-skills title="本地技能">${icon('file')}技能</button>${uiMenu('工具', more)}</div></div></header>
    <div class="tabs mobile-work-tabs" role="group" aria-label="工作区视图"><button data-ui-pane="files">${icon('folder')} 文件</button><button data-ui-pane="editor">${icon('code')} 编辑器</button></div>
    <section class="panel workspace ${w.explorerCollapsed ? 'is-explorer-collapsed' : ''}" data-pane="${w.pane === 'editor' ? 'editor' : 'files'}"${w.explorerWidth ? ` style="--explorer-width:${w.explorerWidth}px"` : ''}><aside class="explorer"><div class="explorer-top"><span>文件目录</span><div><button class="icon-btn" data-action="new-file" aria-label="新建文件">${icon('plus')}</button><button class="icon-btn" data-action="tree-refresh" aria-label="刷新目录">${icon('refresh')}</button></div></div><form id="search-form" class="explorer-tools"><input name="query" placeholder="搜索文件内容" aria-label="搜索项目"><button class="icon-btn" aria-label="执行搜索">${icon('search')}</button></form><div id="file-tree" class="file-tree">${treeHTML()}</div></aside><div class="workspace-resizer" id="workspace-resizer" role="separator" aria-orientation="vertical" aria-label="调节文件树宽度，双击折叠" aria-valuemin="170" aria-valuemax="420" aria-valuenow="${w.explorerCollapsed ? 0 : w.explorerWidth || 240}" tabindex="0"></div><div class="editor-wrap" id="editor-wrap">${editorHTML()}</div></section>
    <div class="taskbar"><select id="task-select" aria-label="选择本机任务"><option value="">选择本机任务</option></select><button class="btn ghost" data-action="load-tasks">刷新任务</button><button class="btn primary" data-action="run-task">${icon('play')}运行</button><button class="btn ghost" data-action="cancel-task">${icon('stop')}停止</button></div><section class="terminal" id="work-terminal" data-size="${esc(w.terminalSize || 'compact')}"><div class="terminal-head"><span>${icon('terminal')}运行输出</span><div class="terminal-controls" role="group" aria-label="输出高度">${[
      ['collapsed', '折叠'],
      ['normal', '常规'],
      ['expanded', '展开'],
    ]
      .map(
        ([size, label]) =>
          `<button type="button" class="terminal-size-btn" data-terminal-size="${size}" aria-pressed="${terminalSizePressed(w.terminalSize, size)}">${label}</button>`,
      )
      .join(
        '',
      )}</div><span id="task-status">${w.operation ? '操作 ' + esc(w.operation.slice(0, 12)) : '等待任务'}</span></div><pre id="task-output">${esc(w.console || '运行任务后，输出将显示在这里。')}</pre></section>` +
    uiHelp(
      '编辑与执行说明',
      '文件保存前需确认差异并通过 SHA 检查，原内容自动备份。编辑器支持完整读取的 UTF-8 文本（上限 1 MiB）；分段读取的文件只读。此处运行本机具名任务；完整 Shell 通过已授权的 MCP <code>shell_exec</code> 调用。',
    )
  );
}
function treeHTML() {
  const w = S.work;
  return `<button class="file-node directory" data-action="tree-home">${icon('folder')}<span>${esc(w.treePath === '.' ? '项目根目录' : w.treePath)}</span></button>${w.tree.map((n) => `<button class="file-node ${n.type === 'directory' ? 'directory' : ''} ${w.path === n.path ? 'selected' : ''}" style="--file-depth:${Math.max(0, n.path.split('/').length - (w.treePath === '.' ? 1 : w.treePath.split('/').length + 1))};padding-left:calc(10px + var(--file-depth) * 12px)" data-action="${n.type === 'directory' ? 'tree-dir' : 'read-file'}" data-path="${esc(n.path)}">${icon(n.type === 'directory' ? 'folder' : 'file')}<span>${esc(n.name)}</span></button>`).join('')}${w.treeNext !== null ? `<button class="btn ghost small" data-action="tree-more">加载更多文件</button>` : ''}${!w.tree.length ? '<div class="empty tiny">选择项目后加载目录</div>' : ''}`;
}
function editorHTML() {
  const w = S.work,
    p = S.projects.find((p) => p.id === w.project);
  if (!w.path)
    return `<div class="workspace-empty"><div>${icon('code')}<h3>选择一个文件</h3><p>浏览目录，或新建文件开始编辑。</p></div></div>`;
  const pathCrumb = w.path
    .split('/')
    .map((part, i, arr) =>
      i === arr.length - 1
        ? `<span class="editor-path-leaf">${esc(part)}</span>`
        : `<button type="button" class="editor-path-directory" data-action="tree-dir" data-path="${esc(arr.slice(0, i + 1).join('/'))}" aria-label="打开目录 ${esc(arr.slice(0, i + 1).join('/'))}">${esc(part)}</button><span class="editor-path-sep" aria-hidden="true">/</span>`,
    )
    .join('');
  return `<div class="editor-top"><div><span class="editor-path" title="${esc(w.path)}">${pathCrumb}</span><span class="editor-status" id="dirty-state" data-dirty="${w.dirty}">${w.dirty ? '未保存' : w.truncated ? '分段只读' : '已同步'}</span></div><div class="actions"><button class="btn ghost small" data-devtools="navigation">代码导航</button><button class="btn ghost small" data-action="reload-file">重新读取</button><button class="btn ghost small" data-action="move-file">移动</button><button class="btn danger small" data-action="delete-file">删除</button><button class="btn primary small" data-action="preview-save" ${p?.mode !== 'write' || w.truncated ? 'disabled' : ''}>${icon('save')}预览并保存</button></div></div>${w.truncated ? notice('此文件超过编辑器读取行数上限，目前只显示前 20,000 行，已禁止保存。MCP 可继续按行读取。') : ''}<div class="editor"><div class="editor-lines" id="editor-lines" aria-hidden="true">${Array.from({ length: Math.min(w.content.split('\n').length, 20001) }, (_, i) => i + 1).join('\n')}</div><textarea id="code-editor" aria-label="源码编辑器" spellcheck="false" autocapitalize="off" autocomplete="off" ${w.truncated || p?.mode !== 'write' ? 'readonly' : ''}>${esc(w.content)}</textarea></div><div class="editor-bottom"><span>UTF-8 · ${esc(p?.alias || '')} · ${w.content.split('\n').length} 行</span><span id="file-sha">SHA ${esc(w.sha === 'new' ? 'NEW FILE' : w.sha.slice(0, 16))}</span></div>`;
}
function editorLineEndings(value, original) {
  return original.includes('\r\n') && !/(^|[^\r])\n/.test(original)
    ? value.replace(/\r\n?/g, '\n').replace(/\n/g, '\r\n')
    : value;
}
function bindEditor() {
  const el = $('#code-editor');
  if (!el) return;
  el.value = S.work.content;
  el.oninput = () => {
    S.editRevision = (S.editRevision || 0) + 1;
    S.work.content = editorLineEndings(el.value, S.work.original);
    S.work.dirty = S.work.content !== S.work.original || S.work.sha === 'new';
    $('#dirty-state').textContent = S.work.dirty ? '未保存' : '已同步';
    $('#dirty-state').dataset.dirty = String(S.work.dirty);
    $('#editor-lines').textContent = Array.from(
      { length: Math.min(el.value.split('\n').length, 20001) },
      (_, i) => i + 1,
    ).join('\n');
  };
  el.onscroll = () => {
    $('#editor-lines').scrollTop = el.scrollTop;
  };
  el.onkeydown = (e) => {
    if (e.key === 'Tab' && !el.readOnly) {
      e.preventDefault();
      const a = el.selectionStart,
        b = el.selectionEnd;
      el.setRangeText('  ', a, b, 'end');
      el.dispatchEvent(new Event('input'));
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      previewSave().catch((err) => toast(err.message, true));
    }
  };
}
// 'compact' is the initial size and reads as "常规" for the toggle state.
function terminalSizePressed(current, size) {
  return (
    (current || 'compact') === size || (size === 'normal' && (current || 'compact') === 'compact')
  );
}
function setTerminalSize(size) {
  S.work.terminalSize = size;
  const terminal = $('#work-terminal');
  if (!terminal) return;
  terminal.dataset.size = size;
  $$('[data-terminal-size]', terminal).forEach((btn) =>
    btn.setAttribute('aria-pressed', String(terminalSizePressed(size, btn.dataset.terminalSize))),
  );
}
function bindWorkbench() {
  bindEditor();
  $('#work-project').onchange = async (e) => {
    const nextProject = e.target.value;
    if (
      S.work.dirty &&
      !(await uiConfirmAction({
        title: '切换项目？',
        body: '切换项目将丢弃尚未保存的草稿。',
        confirmLabel: '丢弃并切换',
        danger: true,
      }))
    ) {
      e.target.value = S.work.project;
      return;
    }
    resetWork(nextProject);
    await renderPage(false);
  };
  $('#search-form').onsubmit = async (e) => {
    e.preventDefault();
    const q = new FormData(e.target).get('query').trim();
    if (q) await searchFiles(q);
  };
  const resizer = $('#workspace-resizer'),
    workspace = $('.workspace');
  if (resizer && workspace) {
    const setWidth = (width) => {
      const clamped = Math.max(170, Math.min(420, Math.round(width)));
      S.work.explorerWidth = clamped;
      S.work.explorerCollapsed = false;
      workspace.classList.remove('is-explorer-collapsed');
      workspace.style.setProperty('--explorer-width', clamped + 'px');
      resizer.setAttribute('aria-valuenow', String(clamped));
    };
    resizer.ondblclick = () => {
      S.work.explorerCollapsed = !S.work.explorerCollapsed;
      workspace.classList.toggle('is-explorer-collapsed', S.work.explorerCollapsed);
      resizer.setAttribute(
        'aria-valuenow',
        String(S.work.explorerCollapsed ? 0 : S.work.explorerWidth || 240),
      );
    };
    // Until the user resizes, the width comes from CSS (it is narrower below 1200px).
    const currentWidth = () =>
      S.work.explorerWidth || $('.explorer', workspace)?.getBoundingClientRect().width || 240;
    resizer.onkeydown = (e) => {
      if (e.key === 'ArrowLeft') setWidth(currentWidth() - 20);
      else if (e.key === 'ArrowRight') setWidth(currentWidth() + 20);
      else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        resizer.ondblclick();
      }
    };
    resizer.onpointerdown = (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const startX = e.clientX,
        startWidth = S.work.explorerCollapsed ? 0 : currentWidth();
      const move = (ev) => setWidth(startWidth + (ev.clientX - startX));
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    };
  }
  $$('[data-terminal-size]').forEach((btn) => {
    btn.onclick = () => setTerminalSize(btn.dataset.terminalSize);
  });
  if (S.work.project && !S.work.tree.length) loadTree().catch((e) => toast(e.message, true));
  if (S.work.operation) pollTask().catch(() => {});
}
async function loadTree(more = false) {
  if (!S.work.project) return;
  const project = S.work.project,
    generation = (S.treeGeneration = (S.treeGeneration || 0) + 1);
  const r = await settled(
    wtool('fs_tree', {
      path: S.work.treePath,
      depth: 2,
      offset: more ? S.work.treeNext || 0 : 0,
      limit: 300,
    }),
  );
  if (project !== S.work.project || generation !== S.treeGeneration) return;
  S.work.tree = more ? S.work.tree.concat(r.entries) : r.entries;
  S.work.treeNext = r.next_offset;
  if ($('#file-tree')) $('#file-tree').innerHTML = treeHTML();
}
async function readFile(path, force = false) {
  if (!S.work.project) throw new Error('请先选择项目');
  if (
    S.work.dirty &&
    !force &&
    !(await uiConfirmAction({
      title: '读取其他文件？',
      body: '读取其他文件将丢弃尚未保存的草稿。',
      confirmLabel: '丢弃并读取',
      danger: true,
    }))
  )
    return false;
  const project = S.work.project,
    revision = S.editRevision || 0,
    generation = (S.readGeneration = (S.readGeneration || 0) + 1);
  const r = await settled(tool('fs_read', { ...workTarget(project), path, max_lines: 20000 }));
  if (project !== S.work.project || generation !== S.readGeneration) return false;
  if (revision !== (S.editRevision || 0)) {
    toast('读取期间有新的编辑，已保留当前草稿。请保存后重新读取。');
    return false;
  }
  S.fileGeneration = (S.fileGeneration || 0) + 1;
  Object.assign(S.work, {
    path: r.path,
    sha: r.sha256,
    content: r.content,
    original: r.content,
    dirty: false,
    truncated: r.truncated,
  });
  if ($('#editor-wrap')) {
    $('#editor-wrap').innerHTML = editorHTML();
    bindEditor();
    $('#file-tree').innerHTML = treeHTML();
  }
  uiSetPane('editor', true);
  return true;
}
async function newFile() {
  if (!S.work.project) throw new Error('请先选择项目');
  modal(
    '新建文件',
    `<div class="field"><label>项目内相对路径</label><input id="new-path" placeholder="例如 src/example.py"><small>父目录必须已存在；需要新目录时可使用下方按钮。</small></div><button class="btn ghost small" id="mkdir">先创建目录</button>`,
    buttons('new-file-confirm', '开始编辑'),
  );
  $('#mkdir').onclick = () =>
    busy($('#mkdir'), async () => {
      const path = $('#new-path').value.trim();
      if (!path) throw new Error('请在输入框填写要创建的目录路径');
      await settled(wtool('fs_mkdir', { path, idempotency_key: uid() }));
      toast('目录已创建');
      await loadTree();
    });
  $('#new-file-confirm').onclick = async () => {
    const path = $('#new-path').value.trim();
    if (!path) return;
    const scope = workDialogScope(),
      revision = S.editRevision || 0;
    if (
      S.work.dirty &&
      !(await uiConfirmAction(
        {
          title: '丢弃当前草稿？',
          body: '当前未保存草稿将被新文件替换。',
          confirmLabel: '丢弃并新建',
          danger: true,
        },
        {
          owner: $('#new-file-confirm'),
          current: () =>
            workDialogCurrent(scope) &&
            revision === (S.editRevision || 0) &&
            $('#new-path')?.value.trim() === path,
        },
      ))
    )
      return;
    S.fileGeneration = (S.fileGeneration || 0) + 1;
    S.readGeneration = (S.readGeneration || 0) + 1;
    Object.assign(S.work, {
      path,
      sha: 'new',
      original: '',
      content: '',
      dirty: true,
      truncated: false,
    });
    closeModal();
    $('#editor-wrap').innerHTML = editorHTML();
    bindEditor();
    uiSetPane('editor', true);
    $('#code-editor')?.focus();
  };
}
// Unified diff → rows. A line is a file header only outside a hunk; hunk
// membership is decided by the remaining old/new counts from the @@ header,
// so content lines such as "--- comment" or "+++i;" stay content.
function diffParse(text) {
  const rows = [];
  let oldLn = 0,
    newLn = 0,
    oldLeft = 0,
    newLeft = 0;
  const lines = String(text || '').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  // Bare +/- text without @@ headers: colour it, but show no line numbers.
  const bare = !lines.some((t) => t.startsWith('@@ '));
  if (bare) oldLeft = newLeft = Infinity;
  for (const t of lines) {
    const hunk = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(t);
    if (hunk) {
      oldLn = Number(hunk[1]);
      oldLeft = hunk[2] === undefined ? 1 : Number(hunk[2]);
      newLn = Number(hunk[3]);
      newLeft = hunk[4] === undefined ? 1 : Number(hunk[4]);
      rows.push({ kind: 'hunk', text: t, a: '', b: '' });
      continue;
    }
    if (t.startsWith('\\')) {
      rows.push({ kind: 'note', text: t, a: '', b: '' });
      continue;
    }
    if (oldLeft <= 0 && newLeft <= 0) {
      rows.push({ kind: 'head', text: t, a: '', b: '' });
      continue;
    }
    if (t.startsWith('+') && newLeft > 0) {
      rows.push({ kind: 'plus', text: t, a: '', b: bare ? '' : newLn++ });
      newLeft--;
    } else if (t.startsWith('-') && oldLeft > 0) {
      rows.push({ kind: 'minus', text: t, a: bare ? '' : oldLn++, b: '' });
      oldLeft--;
    } else {
      rows.push({ kind: 'ctx', text: t, a: bare ? '' : oldLn++, b: bare ? '' : newLn++ });
      oldLeft--;
      newLeft--;
    }
  }
  return rows;
}
// Highlight the changed middle of a removed/added line pair. Skipped when the
// lines share too little to make the highlight meaningful.
function diffWordPair(minus, plus) {
  const a = minus.slice(1),
    b = plus.slice(1),
    max = Math.min(a.length, b.length);
  let p = 0;
  while (p < max && a[p] === b[p]) p++;
  while (p > 0 && /\w/.test(a[p - 1]) && (/\w/.test(a[p] || '') || /\w/.test(b[p] || ''))) p--;
  let s = 0;
  while (s < max - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  while (
    s > 0 &&
    /\w/.test(a[a.length - s]) &&
    (/\w/.test(a[a.length - 1 - s] || '') || /\w/.test(b[b.length - 1 - s] || ''))
  )
    s--;
  if ((p + s) * 2 < Math.max(a.length, b.length) || p + s === 0) return null;
  const wrap = (sign, t) => {
    const mid = t.slice(p, t.length - s);
    return `${esc(sign + t.slice(0, p))}${mid ? `<mark class="diff-word">${esc(mid)}</mark>` : ''}${esc(t.slice(t.length - s))}`;
  };
  return [wrap(minus[0], a), wrap(plus[0], b)];
}
function diffRowsHTML(text) {
  const rows = diffParse(text);
  const html = rows.map((r) => esc(r.text));
  for (let i = 0; i < rows.length;) {
    if (rows[i].kind !== 'minus') {
      i++;
      continue;
    }
    let m = i;
    while (m < rows.length && rows[m].kind === 'minus') m++;
    let n = m;
    while (n < rows.length && rows[n].kind === 'plus') n++;
    for (let k = 0; k < Math.min(m - i, n - m); k++) {
      const pair = diffWordPair(rows[i + k].text, rows[m + k].text);
      if (pair) [html[i + k], html[m + k]] = pair;
    }
    i = n;
  }
  return rows
    .map((r, i) => {
      const cls = r.kind === 'head' ? 'hunk head' : r.kind === 'note' ? 'ctx note' : r.kind;
      const a = r.kind === 'hunk' ? '…' : r.a,
        b = r.kind === 'hunk' ? '…' : r.b;
      return `<div class="diff-line ${cls}"><span class="diff-ln">${a}</span><span class="diff-ln">${b}</span><span class="diff-code">${html[i]}</span></div>`;
    })
    .join('');
}
function diffHTML(d) {
  const added = d.added_lines || 0,
    removed = d.removed_lines || 0,
    total = Math.max(1, added + removed);
  const plusPct = Math.round((added / total) * 100),
    minusPct = Math.round((removed / total) * 100);
  const rows = d.diff
    ? diffRowsHTML(d.diff)
    : '<div class="diff-line ctx"><span class="diff-ln"></span><span class="diff-ln"></span><span class="diff-code">没有内容差异</span></div>';
  return `<div class="diff-stats"><span>+ ${added} 行</span><span>− ${removed} 行</span><span class="diff-bar" aria-hidden="true"><i class="diff-bar-plus" style="width:${plusPct}%"></i><i class="diff-bar-minus" style="width:${minusPct}%"></i></span>${d.diff_truncated ? '<small>差异已截断，保存前请在本机核对完整内容</small>' : ''}</div><div class="code-box diff">${rows}</div>`;
}
async function previewSave() {
  const w = S.work;
  if (!w.path || w.truncated || S.previewing) return;
  const snapshot = {
      ...workTarget(w.project),
      path: w.path,
      expected_sha256: w.sha,
      content: w.content,
      idempotency_key: uid(),
    },
    generation = S.fileGeneration || 0,
    scope = workDialogScope();
  S.previewing = true;
  try {
    const d = await settled(
      tool('fs_preview', {
        project: snapshot.project,
        workspace_id: snapshot.workspace_id,
        path: snapshot.path,
        content: snapshot.content,
      }),
    );
    if (
      !workDialogCurrent(scope) ||
      S.work.project !== snapshot.project ||
      S.work.path !== snapshot.path ||
      generation !== (S.fileGeneration || 0)
    )
      return;
    if (S.work.content !== snapshot.content) {
      toast('预览期间内容已更新，请重新预览当前草稿。');
      return;
    }
    const dialog = modal(
      '确认写入 · ' + snapshot.path,
      `${diffHTML(d)}<p class="form-note">只有当前文件 SHA 仍与读取时一致，才会执行写入。修改前会保存本机备份。${d.diff_truncated ? '当前差异预览不完整，请取消并在本机检查。' : ''}</p>`,
      buttons('confirm-save', '确认写入'),
      true,
    );
    const button = $('#confirm-save'),
      displayed = Object.freeze({ ...scope, modalIntent: S.modalIntent || 0 });
    button.disabled = !!d.diff_truncated;
    button.onclick = () =>
      busy(button, async () => {
        if (
          !workDialogCurrent(displayed) ||
          S.work.path !== snapshot.path ||
          generation !== (S.fileGeneration || 0)
        )
          throw new Error('当前文件或确认窗口已变化，请重新预览。');
        const r = await settled(tool('fs_write', snapshot));
        if (
          S.work.project === snapshot.project &&
          S.work.path === snapshot.path &&
          generation === (S.fileGeneration || 0)
        ) {
          S.work.sha = r.sha256;
          S.work.original = snapshot.content;
          S.work.dirty = S.work.content !== snapshot.content;
          if ($('#editor-wrap')) {
            $('#editor-wrap').innerHTML = editorHTML();
            bindEditor();
          }
        }
        closeModal(dialog);
        toast('已写回本机，修改前内容已备份');
        await loadTree();
      });
  } catch (error) {
    if (workDialogCurrent(scope)) throw error;
  } finally {
    S.previewing = false;
  }
}
async function deleteFile() {
  const snapshot = { ...workTarget(), path: S.work.path, sha: S.work.sha, content: S.work.content },
    generation = S.fileGeneration || 0;
  if (!snapshot.path || snapshot.sha === 'new') throw new Error('请先读取一个已存在的文件');
  if (
    !(await uiConfirmAction(
      {
        title: `删除 ${snapshot.path}？`,
        body: '仅删除该文件，修改前会保存备份。',
        confirmLabel: '删除文件',
        danger: true,
      },
      {
        current: () =>
          S.work.project === snapshot.project &&
          (S.work.workspace_id || '') === snapshot.workspace_id &&
          S.work.path === snapshot.path &&
          (S.fileGeneration || 0) === generation &&
          S.work.content === snapshot.content,
      },
    ))
  )
    return;
  await settled(
    tool('fs_delete', {
      project: snapshot.project,
      workspace_id: snapshot.workspace_id,
      path: snapshot.path,
      expected_sha256: snapshot.sha,
      idempotency_key: uid(),
    }),
  );
  if (
    S.work.project === snapshot.project &&
    S.work.path === snapshot.path &&
    generation === (S.fileGeneration || 0)
  ) {
    S.fileGeneration = (S.fileGeneration || 0) + 1;
    S.readGeneration = (S.readGeneration || 0) + 1;
    if (S.work.content === snapshot.content)
      Object.assign(S.work, { path: '', sha: '', content: '', original: '', dirty: false });
    else Object.assign(S.work, { sha: 'new', original: '', dirty: true }); // retain text typed while waiting
    if ($('#editor-wrap')) {
      $('#editor-wrap').innerHTML = editorHTML();
      bindEditor();
    }
    await loadTree();
  }
  toast('文件已删除，本机备份可恢复');
}
async function moveFile() {
  const snapshot = {
      ...workTarget(),
      path: S.work.path,
      sha: S.work.sha,
      original: S.work.original,
    },
    generation = S.fileGeneration || 0;
  if (!snapshot.path || snapshot.sha === 'new') throw new Error('请先读取一个已存在的文件');
  if (S.work.dirty) throw new Error('请先保存或放弃草稿后再移动');
  const dialog = modal(
    '移动文件',
    `<div class="field"><label>目标相对路径（必须尚不存在）</label><input id="move-destination" value="${esc(snapshot.path)}"></div>`,
    buttons('confirm-move', '移动'),
  );
  const button = $('#confirm-move');
  button.onclick = () =>
    busy(button, async () => {
      const destination = $('#move-destination', dialog).value;
      const r = await settled(
        tool('fs_move', {
          project: snapshot.project,
          workspace_id: snapshot.workspace_id,
          path: snapshot.path,
          destination,
          expected_sha256: snapshot.sha,
          idempotency_key: uid(),
        }),
      );
      closeModal(dialog);
      if (
        S.work.project === snapshot.project &&
        S.work.path === snapshot.path &&
        generation === (S.fileGeneration || 0)
      ) {
        S.fileGeneration = (S.fileGeneration || 0) + 1;
        S.readGeneration = (S.readGeneration || 0) + 1;
        Object.assign(S.work, {
          path: r.destination,
          sha: r.sha256,
          original: snapshot.original,
          dirty: S.work.content !== snapshot.original,
        });
        if ($('#editor-wrap')) {
          $('#editor-wrap').innerHTML = editorHTML();
          bindEditor();
        }
        await loadTree();
      }
      toast('文件已移动');
    });
}
// One identity spans the request and its rendered controls. A later request,
// workspace reset, navigation, session change or modal dismissal invalidates it.
function workDialogScope(extra = {}) {
  return Object.freeze({
    ...workTarget(),
    workspace_id: S.work.workspace_id || '',
    session: S.session,
    page: S.page,
    pageEpoch: S.renderSeq,
    workEpoch: S.workGeneration || 0,
    modalIntent: S.modalIntent || 0,
    request: (S.workDialogGeneration = (S.workDialogGeneration || 0) + 1),
    ...extra,
  });
}
function workDialogCurrent(scope) {
  return S.page === 'workbench' && panelDialogCurrent(scope);
}
function panelDialogCurrent(scope) {
  return (
    !!scope &&
    !!scope.session &&
    S.session === scope.session &&
    S.page === scope.page &&
    S.renderSeq === scope.pageEpoch &&
    S.work.project === scope.project &&
    (S.work.workspace_id || '') === scope.workspace_id &&
    (S.workGeneration || 0) === scope.workEpoch &&
    S.workDialogGeneration === scope.request &&
    (S.modalIntent || 0) === scope.modalIntent
  );
}
async function searchFiles(query, offset = 0, previous = null) {
  if (!S.work.project || (previous && !workDialogCurrent(previous))) return;
  const scope = workDialogScope({ query });
  try {
    const r = await settled(
      tool('fs_search', {
        project: scope.project,
        workspace_id: scope.workspace_id,
        query,
        offset,
        limit: 100,
      }),
    );
    if (!workDialogCurrent(scope)) return;
    const dialog = modal(
      '搜索 · ' + query,
      `<p class="form-note">${esc(json({ matches: r.matches?.length, files_scanned: r.scanned_files, truncated: r.truncated }))}</p>${(r.matches || []).map((x) => `<button class="search-hit" data-action="search-read" data-path="${esc(x.path)}"><code>${esc(x.path)} : ${x.line}</code><p>${esc(x.text)}</p></button>`).join('') || empty('没有找到匹配项。')}<p class="form-note">${r.truncated ? '搜索已到达返回数或扫描预算限制，不代表全仓已扫描。' : '此结果仅覆盖文件工具允许访问的文本文件。'}</p>`,
      r.next_offset !== null && r.next_offset !== undefined
        ? '<button class="btn" id="search-more">下一页结果</button>'
        : '',
      true,
    );
    const displayed = Object.freeze({ ...scope, modalIntent: S.modalIntent || 0 });
    for (const hit of $$('.search-hit', dialog)) hit.workScope = displayed;
    const more = $('#search-more', dialog);
    if (more) more.onclick = () => searchFiles(query, r.next_offset, displayed);
  } catch (e) {
    if (workDialogCurrent(scope)) toast(e.message, true);
  }
}
async function historyModal() {
  if (!S.work.project) throw new Error('请先选择项目');
  const scope = workDialogScope({ path: S.work.path || '' });
  let r;
  try {
    r = await settled(
      tool('history_list', {
        project: scope.project,
        workspace_id: scope.workspace_id,
        path: scope.path,
        limit: 50,
      }),
    );
  } catch (error) {
    if (workDialogCurrent(scope)) throw error;
    return;
  }
  if (!workDialogCurrent(scope)) return;
  const rows = r.backups || r.entries || [];
  const dialog = modal(
    '本机修改备份',
    `<p class="form-note">恢复某次操作前的文件内容。恢复也会建立新的备份；移动文件的备份只恢复原路径，不会自动删除新路径。</p>${rows.length ? rows.map((b) => `<div class="history-row"><div><code>${esc(b.path)}</code><p>${esc(timeText(b.at))} · ${esc(b.id.slice(0, 10))}</p></div><button class="btn small" data-action="restore-backup" data-id="${esc(b.id)}" data-path="${esc(b.path)}">恢复前版本</button></div>`).join('') : empty('这个范围还没有文件备份。')}`,
    '',
    true,
  );
  const displayed = Object.freeze({ ...scope, modalIntent: S.modalIntent || 0 });
  for (const button of $$('[data-action=restore-backup]', dialog)) button.workScope = displayed;
}
async function restoreBackup(id, path, scope) {
  if (S.restoring) return;
  if (!workDialogCurrent(scope))
    throw new Error('当前项目、工作目录或备份窗口已经变化，请重新打开备份。');
  const { project, workspace_id } = scope;
  const dialog = $('.modal'),
    generation = S.fileGeneration || 0,
    revision = S.editRevision || 0;
  if (project !== S.work.project || workspace_id !== (S.work.workspace_id || ''))
    throw new Error('当前项目或工作目录已经变化，请重新打开备份。');
  if (
    S.work.dirty &&
    !(await uiConfirmAction(
      {
        title: '放弃草稿并恢复？',
        body: '恢复操作将重新读取文件，当前草稿可能丢失。继续？',
        confirmLabel: '继续恢复',
        danger: true,
      },
      {
        owner: dialog,
        current: () =>
          workDialogCurrent(scope) &&
          !S.restoring &&
          generation === (S.fileGeneration || 0) &&
          revision === (S.editRevision || 0),
      },
    ))
  )
    return;
  S.restoring = true;
  try {
    let sha = 'new';
    try {
      sha = (await settled(tool('fs_read', { project, workspace_id, path, max_lines: 1 }))).sha256;
    } catch (error) {
      if (error.code !== 'NOT_FOUND') throw error;
    }
    if (
      !workDialogCurrent(scope) ||
      $('.modal') !== dialog ||
      !dialog?.isConnected ||
      generation !== (S.fileGeneration || 0) ||
      revision !== (S.editRevision || 0)
    )
      throw new Error('编辑内容已经变化，已取消本次恢复，请重新核对。');
    if (
      !(await uiConfirmAction(
        {
          title: '恢复文件版本？',
          body: `把 ${path} 恢复到这次操作之前的内容？`,
          confirmLabel: '恢复版本',
          danger: true,
        },
        {
          owner: dialog,
          current: () =>
            workDialogCurrent(scope) &&
            generation === (S.fileGeneration || 0) &&
            revision === (S.editRevision || 0),
        },
      ))
    )
      return;
    await settled(
      tool('history_restore', {
        project,
        workspace_id,
        backup_id: id,
        expected_sha256: sha,
        idempotency_key: uid(),
      }),
    );
    closeModal(dialog);
    if (
      S.session === scope.session &&
      S.work.project === project &&
      (S.work.workspace_id || '') === workspace_id &&
      (S.workGeneration || 0) === scope.workEpoch &&
      generation === (S.fileGeneration || 0) &&
      revision === (S.editRevision || 0)
    ) {
      try {
        await readFile(path, true);
      } catch (error) {
        if (error.code !== 'NOT_FOUND') throw error;
        if (
          S.session === scope.session &&
          S.work.project === project &&
          (S.work.workspace_id || '') === workspace_id &&
          (S.workGeneration || 0) === scope.workEpoch &&
          generation === (S.fileGeneration || 0) &&
          revision === (S.editRevision || 0)
        ) {
          S.fileGeneration = (S.fileGeneration || 0) + 1;
          Object.assign(S.work, {
            path: '',
            content: '',
            original: '',
            sha: '',
            dirty: false,
            truncated: false,
          });
          if ($('#editor-wrap')) $('#editor-wrap').innerHTML = editorHTML();
        }
      }
      await loadTree();
    } else toast('恢复已完成，等待期间的新草稿已保留；保存前请重新核对本机文件。');
    toast('恢复已完成');
  } finally {
    S.restoring = false;
  }
}
async function checkpoint() {
  if (!S.work.project) throw new Error('请先选择项目');
  const scope = workDialogScope();
  if (
    !(await uiConfirmAction(
      {
        title: '建立源码检查点？',
        body: '为当前项目建立本机源码 ZIP 检查点？不包含依赖、凭据、链接或超过限制的文件，也不是整个硬盘备份。',
        confirmLabel: '建立检查点',
      },
      { current: () => workDialogCurrent(scope) },
    ))
  )
    return;
  let r;
  try {
    r = await settled(
      wtool('project_checkpoint', { label: 'Panel checkpoint', idempotency_key: uid() }),
    );
  } catch (error) {
    if (workDialogCurrent(scope)) throw error;
    return;
  }
  if (!workDialogCurrent(scope)) return;
  modal(
    '源码检查点已保存',
    `${notice('检查点保存于家里 Agent 的状态目录。它是尽力读取的源码归档，不是原子文件系统快照。', true)}<div class="spacer"></div><div class="code-box"><pre>${esc(json(r))}</pre></div>`,
    '',
    true,
  );
}
async function gitView(which) {
  if (!S.work.project) throw new Error('请先选择项目');
  const scope = workDialogScope(),
    path = S.work.path,
    generation = S.fileGeneration || 0;
  const current = () =>
    workDialogCurrent(scope) && S.work.path === path && (S.fileGeneration || 0) === generation;
  let r;
  try {
    r = await settled(wtool(which, which === 'git_diff' ? { staged: false } : {}));
  } catch (error) {
    if (current()) throw error;
    return;
  }
  if (!current()) return;
  modal(
    which === 'git_status' ? 'Git 状态' : 'Git 工作区差异',
    `<div class="code-box"><pre>${esc(r.output || '没有工作区改动')}</pre></div>`,
    '',
    true,
  );
}
async function loadTasks() {
  const project = S.work.project;
  const r = await settled(tool('tasks_list', workTarget(project)));
  if (project !== S.work.project || !$('#task-select')) return;
  const tasks = r.tasks || [];
  $('#task-select').innerHTML =
    '<option value="">选择本机授权任务</option>' +
    tasks
      .map(
        (t) =>
          `<option value="${esc(t.name)}">${esc(t.name)}${t.executable_available === false ? ' · 本机命令未找到' : ''}${t.description ? ' · ' + esc(t.description) : ''}</option>`,
      )
      .join('');
  if (!tasks.length)
    toast('没有可运行任务。请在本机配置 tasks，并同时开启本机目录和项目的任务权限。');
}
function trackTask(id) {
  S.work.operation = id;
  taskSessionValue('codepier-operation', id);
}
function clearTaskSubmission(submission) {
  if (S.taskSubmission !== submission) return;
  S.taskSubmission = null;
  taskSessionValue('codepier-task-submission', null);
}
async function runTask() {
  if (S.taskSubmitting) return;
  S.taskSubmitting = true;
  try {
    restoreTaskSubmission();
    let submission = S.taskSubmission;
    if (!submission) {
      const name = $('#task-select')?.value,
        project = S.work.project;
      if (!name || !project) throw new Error('请先刷新并选择本机任务');
      if (S.work.operation) {
        try {
          const old = await api('/api/operations/' + S.work.operation);
          if (old.pending) throw new Error('当前任务仍在执行或恢复连接，请打开任务查看进度。');
        } catch (error) {
          if (error.code !== 'OPERATION_NOT_FOUND') throw error;
          trackTask(null);
        }
      }
      const scope = workDialogScope();
      if (
        !(await uiConfirmAction(
          {
            title: '运行本机任务？',
            body: `运行本机任务 ${name}？这会执行本机预先定义的程序。`,
            confirmLabel: '运行任务',
          },
          {
            current: () =>
              workDialogCurrent(scope) &&
              S.work.project === project &&
              $('#task-select')?.value === name,
          },
        ))
      )
        return;
      submission = { ...workTarget(project), task: name, idempotency_key: uid() };
      S.taskSubmission = submission;
      // Keep the original key even when every reply is lost, including a reload.
      taskSessionValue('codepier-task-submission', JSON.stringify(submission));
    } else toast('正在核实上次提交的任务 ' + submission.task + '，沿用原操作幂等键。');
    try {
      const r = await tool('tasks_run', submission);
      if (S.taskSubmission !== submission) return;
      trackTask(r.operation_id);
      clearTaskSubmission(submission);
      S.work.console = '任务已保存，等待 Agent 输出…';
      if (S.work.terminalSize === 'collapsed' || S.work.terminalSize === 'compact')
        setTerminalSize('normal');
      if ($('#task-output')) $('#task-output').textContent = S.work.console;
      await pollTask();
    } catch (error) {
      if (S.taskSubmission === submission) {
        if (error.operation_id) {
          trackTask(error.operation_id);
          clearTaskSubmission(submission);
          await pollTask();
        } else if (
          error.code &&
          error.status >= 400 &&
          error.status < 500 &&
          ![401, 408, 429].includes(error.status) &&
          !error.retryable
        ) {
          // A policy change may reject today's retry after yesterday's request
          // already started. Resolve its saved key before releasing the receipt.
          const found = await tool('operations_list', {
            idempotency_key: submission.idempotency_key,
            limit: 1,
          });
          if (found.operations?.length) trackTask(found.operations[0].id);
          clearTaskSubmission(submission);
          if (found.operations?.length) await pollTask();
        }
      }
      throw error;
    }
  } finally {
    S.taskSubmitting = false;
  }
}
async function pollTask() {
  const id = S.work.operation,
    session = S.session;
  if (!id || !session || (S.taskPolling?.id === id && S.taskPolling?.session === session)) return;
  const poll = { id, session };
  S.taskPolling = poll;
  clearTimeout(S.poll);
  S.poll = null;
  const current = () => S.work.operation === id && S.session === session;
  try {
    const r = await api('/api/operations/' + id);
    if (!current()) return;
    S.work.console =
      r.output ||
      r.result?.data?.output ||
      r.error ||
      (r.pending ? '等待本机输出；网络中断不代表任务失败…' : '任务已结束，无输出。');
    if ($('#task-output')) {
      $('#task-output').textContent = S.work.console;
      $('#task-output').scrollTop = $('#task-output').scrollHeight;
    }
    if ($('#task-status'))
      $('#task-status').textContent = (stateNames[r.state] || r.state) + ' · ' + r.id.slice(0, 12);
    S.taskPollFailures = 0;
    if (r.pending) S.poll = setTimeout(() => pollTask().catch(() => {}), 1500);
  } catch (error) {
    if (!current()) return;
    if (error.code === 'OPERATION_NOT_FOUND') {
      trackTask(null);
      S.work.console = '已停止跟踪不存在的任务 ' + id + '；请在审计中核实原执行结果。';
      if ($('#task-output')) $('#task-output').textContent = S.work.console;
      if ($('#task-status')) $('#task-status').textContent = '原任务记录不存在';
      toast(S.work.console, true);
      return;
    }
    if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
      if ($('#task-status')) $('#task-status').textContent = error.message;
      return;
    }
    S.taskPollFailures = (S.taskPollFailures || 0) + 1;
    if ($('#task-status'))
      $('#task-status').textContent = '连接恢复中 · 保留任务 ' + id.slice(0, 12);
    S.poll = setTimeout(
      () => pollTask().catch(() => {}),
      Math.min(10000, 1500 * 2 ** Math.min(3, S.taskPollFailures)),
    );
  } finally {
    if (S.taskPolling === poll) S.taskPolling = null;
  }
}
async function auditHTML(seq = S.renderSeq) {
  if (S.auditMode === 'operations' && window.CodePierCallLog) return CodePierCallLog.html(seq);
  const type = S.auditMode;
  const query = new URLSearchParams({
    limit: '40',
    offset: String(S.auditOffset),
    source: S.auditSource,
    status: S.auditStatus,
    ...(type === 'events' ? { q: S.auditQuery } : {}),
  });
  const r = await api('/api/' + (type === 'operations' ? 'operations' : 'audit') + '?' + query);
  const rows = type === 'operations' ? r.operations : r.events;
  if (seq === S.renderSeq) {
    S.auditNext = r.next_offset;
    S.auditRows = rows;
  }
  return (
    heading(
      '操作审计',
      '调用记录',
      '',
      `<button class="btn ghost" data-action="export-audit">${icon('download')}导出审计 CSV</button>`,
    ) +
    `<div class="filters"><div class="tabs"><button class="${type === 'operations' ? 'active' : ''}" data-action="audit-mode" data-mode="operations">工具执行</button><button class="${type === 'events' ? 'active' : ''}" data-action="audit-mode" data-mode="events">全部事件</button></div><select id="audit-source" aria-label="来源"><option value="">全部来源</option>${['mcp', 'panel', ...(type === 'events' ? ['device'] : [])].map((v) => `<option value="${v}" ${S.auditSource === v ? 'selected' : ''}>${v.toUpperCase()}</option>`).join('')}</select><select id="audit-status" aria-label="操作状态"><option value="">全部状态</option>${(type === 'operations' ? ['queued', 'running', 'reconnecting', 'cancelling', 'succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'] : ['ok', 'started', 'denied', 'failed']).map((v) => `<option value="${v}" ${S.auditStatus === v ? 'selected' : ''}>${stateNames[v] || v}</option>`).join('')}</select>${type === 'events' ? `<input id="audit-query" value="${esc(S.auditQuery)}" placeholder="搜索调用者 / 操作 / 目标"><button class="btn small" data-action="audit-search">搜索</button>` : ''}</div><section class="panel">${type === 'operations' ? operationsTable(rows) : rows.length ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>时间</th><th>调用来源</th><th>事件</th><th>目标</th><th>状态</th><th></th></tr></thead><tbody>${rows.map((r, i) => `<tr><td class="mono tiny muted">${esc(timeText(r.at))}</td><td class="tiny">${esc(r.actor)}</td><td class="mono tiny">${esc(r.action)}</td><td>${esc(r.target || '—')}</td><td>${badge(r.status)}</td><td><button class="icon-btn" data-event-index="${i}" aria-label="查看审计事件">${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>` : empty('当前筛选范围没有记录。')}<div class="pagination"><span>${rows.length ? `第 ${S.auditOffset + 1}–${S.auditOffset + rows.length} 条` : '暂无记录'}</span><div class="actions"><button class="btn ghost small" data-action="audit-prev" ${S.auditOffset === 0 ? 'disabled' : ''}>上一页</button><button class="btn ghost small" data-action="audit-next" ${r.next_offset === null ? 'disabled' : ''}>下一页</button></div></div></section>${uiHelp('记录范围', '日志保存工具调用与执行证据，不含完整对话；服务器管理员可修改记录，不是第三方审计存证。')}`
  );
}
function bindAudit() {
  if (S.auditMode === 'operations' && window.CodePierCallLog) return CodePierCallLog.bind();
  $('#audit-source').onchange = (e) => {
    S.auditSource = e.target.value;
    S.auditOffset = 0;
    renderPage(false);
  };
  $('#audit-status').onchange = (e) => {
    S.auditStatus = e.target.value;
    S.auditOffset = 0;
    renderPage(false);
  };
  $$('[data-event-index]').forEach(
    (b) =>
      (b.onclick = () => {
        const r = S.auditRows[Number(b.dataset.eventIndex)];
        modal(
          r.action,
          `<dl class="kv"><dt>调用者</dt><dd>${esc(r.actor)}</dd><dt>时间</dt><dd>${esc(timeText(r.at))}</dd><dt>目标</dt><dd>${esc(r.target)}</dd></dl><div class="code-box"><pre>${esc(json(r.detail))}</pre></div>`,
          '',
          true,
        );
      }),
  );
}
async function operationDetail(id) {
  const scope = workDialogScope();
  let r;
  try {
    r = await api('/api/operations/' + id);
  } catch (error) {
    if (panelDialogCurrent(scope)) throw error;
    return;
  }
  if (!panelDialogCurrent(scope)) return;
  modal(
    '操作详情 · ' + r.tool,
    `<dl class="kv"><dt>操作编号</dt><dd><code>${esc(r.id)}</code></dd><dt>状态</dt><dd>${badge(r.state)}</dd><dt>调用者</dt><dd>${esc(r.actor)}</dd><dt>投递次数</dt><dd>${r.attempts || 0}（重投不会创建新操作）</dd><dt>首次执行期限</dt><dd>${esc(timeText(r.deadline))}</dd><dt>恢复状态</dt><dd>${esc(r.transport_error || '无网络异常')}${r.cancel_requested ? ' · 已保存取消请求' : ''}</dd><dt>开始 / 更新</dt><dd>${esc(timeText(r.created))}<br>${esc(timeText(r.updated))}</dd></dl>${window.CodePierQueue?.waitingHTML(r) || ''}${window.CodePierQueue?.liveHTML(r) || ''}${window.CodePierQueue?.runHTML(r) || ''}${r.error ? notice(esc(r.error)) : ''}<div class="audit-detail-title">参数摘要</div><div class="code-box"><pre>${esc(json(r.args_summary))}</pre></div>${r.result?.data?.diff !== undefined ? `<div class="audit-detail-title">文件差异</div>${diffHTML(r.result.data)}` : ''}${r.output ? `<div class="audit-detail-title">任务输出（保留尾部）</div><div class="code-box"><pre>${esc(r.output)}</pre></div>` : ''}<div class="audit-detail-title">实际返回结果</div><div class="code-box"><pre>${esc(json(r.result))}</pre></div>`,
    `<button class="btn ghost" data-insight="trace" data-id="${esc(id)}">${icon('activity')}执行链路</button><button class="btn ghost" id="refresh-operation">${icon('refresh')}刷新结果</button><button class="btn primary" data-action="close-modal">关闭</button>`,
    true,
  );
  $('#refresh-operation').onclick = () => busy($('#refresh-operation'), () => operationDetail(id));
}
function codingEndpoint() {
  const url = new URL(S.settings.mcp_url, location.href);
  url.searchParams.set('profile', 'core');
  return url.href;
}
function grantListHTML() {
  const now = Date.now() / 1000;
  const historical = (g) =>
    Boolean(
      g.revoked ||
      g.status === 'expired' ||
      (g.expires && g.expires <= now && g.status !== 'pending'),
    );
  const current = S.grants.filter((g) => !historical(g));
  const history = S.grants.filter(historical);
  const row = (g) => {
    const expired =
      g.status === 'expired' || Boolean(g.expires && g.expires <= now && g.status !== 'pending');
    const status = g.revoked
      ? '已撤销'
      : expired
        ? '已过期'
        : g.status === 'pending' || (!g.expires && !g.unlimited)
          ? '待连接'
          : '';
    const projects = g.projects.map((id) => S.projects.find((p) => p.id === id)?.alias || id);
    const scopeNames = { read: '读取', write: '写入', execute: '运行命令', computer: '桌面控制' };
    const range =
      g.authorization_mode === 'role'
        ? '<span>权限随角色变化</span>'
        : g.projects.includes('*')
          ? '<span>全部现有及未来项目</span>'
          : `<details class="grant-projects"><summary>${projects.length} 个项目</summary><div>${projects.map((name) => `<span>${esc(name)}</span>`).join('')}</div></details>`;
    return `<article class="grant-row ${historical(g) ? 'grant-inactive' : ''}" data-grant-id="${esc(g.id)}"><div class="grant-info"><h3><span>${esc(g.label)}</span><span class="badge purple">${g.client_id ? '连接' : '访问令牌'}</span>${status ? `<span class="badge neutral">${status}</span>` : ''}</h3><div class="grant-meta"><span>${g.authorization_mode === 'role' ? '角色授权' : g.profile_id ? '访问配置' : '固定授权'}</span><span>权限：${esc(g.scopes.map((scope) => scopeNames[scope] || scope).join(' · '))}</span>${range}</div><p>${g.status === 'pending' ? '等待客户端完成连接' : g.unlimited ? '无限期，直到手动撤销' : g.expires ? `${expired ? '已于' : '到期'} ${esc(timeText(g.expires))}${expired ? ' 过期' : ''}` : expired ? '已无有效凭据' : '尚未连接'}</p></div><div class="actions">${
      g.revoked
        ? `<button class="btn small" data-action="grant-access-preview" data-id="${esc(g.id)}">查看</button>`
        : `<button class="btn small" data-action="edit-grant" data-id="${esc(g.id)}">编辑</button><button class="btn danger small" data-action="revoke-grant" data-id="${esc(g.id)}">撤销</button>`
    }</div></article>`;
  };
  const list = (items, group) => {
    const size = 5;
    const pages = Math.max(1, Math.ceil(items.length / size));
    const page = Math.max(0, Math.min(S.grantPages[group], pages - 1));
    S.grantPages[group] = page;
    return `<div class="grant-list" data-grant-group="${group}">${items
      .slice(page * size, (page + 1) * size)
      .map(row)
      .join(
        '',
      )}</div>${pages > 1 ? `<nav class="pagination grant-pagination" aria-label="${group === 'current' ? '当前' : '历史'}授权分页"><span role="status">${page * size + 1}–${Math.min((page + 1) * size, items.length)} / ${items.length}</span><div class="actions"><button class="btn ghost small" data-action="grant-page" data-group="${group}" data-delta="-1" ${page === 0 ? 'disabled' : ''}>上一页</button><button class="btn ghost small" data-action="grant-page" data-group="${group}" data-delta="1" ${page === pages - 1 ? 'disabled' : ''}>下一页</button></div></nav>` : ''}`;
  };
  return `<div class="panel-head"><h2>${icon('key')}已有连接</h2><span class="badge neutral">${current.length} 个当前连接</span></div><div class="panel-body">${current.length ? list(current, 'current') : empty('暂无有效连接。')}${history.length ? `<div class="grant-history"><button class="btn ghost grant-history-toggle" data-action="grant-history" aria-expanded="${S.grantHistoryOpen}" aria-controls="grant-history-list">${S.grantHistoryOpen ? '收起' : '查看'}历史授权 <span class="badge neutral">${history.length}</span><span class="muted">已撤销 / 已过期</span></button><div id="grant-history-list" ${S.grantHistoryOpen ? '' : 'hidden'}>${S.grantHistoryOpen ? list(history, 'history') : ''}</div></div>` : ''}</div>`;
}
function refreshGrantList(button) {
  const focusAction = button.dataset.action;
  const group = button.dataset.group;
  const delta = button.dataset.delta;
  const panel = $('#grant-panel');
  if (!panel) return;
  panel.innerHTML = grantListHTML();
  const selector = `[data-action="${focusAction}"]${group ? `[data-group="${group}"][data-delta="${delta}"]` : ''}`;
  const next = panel.querySelector(selector);
  if (next && !next.disabled) next.focus({ preventScroll: true });
  else
    panel
      .querySelector(`[data-action="grant-page"][data-group="${group}"]:not(:disabled)`)
      ?.focus({ preventScroll: true });
}
function connectHTML() {
  const settings = S.settings;
  return (
    heading('连接') +
    `<section class="panel"><div class="panel-head"><h2>ChatGPT 连接地址</h2></div><div class="panel-body">
      <div class="endpoint"><code>${esc(settings.mcp_url)}</code><button class="icon-btn" data-action="copy-endpoint" aria-label="复制 MCP 地址">${icon('copy')}</button></div>
      <p>在 ChatGPT 中添加此地址。直连使用公开 HTTPS，私有部署可使用 Secure MCP Tunnel。</p>
      <details class="form-advanced" id="other-clients"><summary>其他客户端</summary>
        <div class="actions"><button class="btn small" data-action="copy-coding-endpoint">复制核心地址</button><button class="btn small" data-action="copy-role-endpoint">复制角色地址</button><button class="btn small" data-action="new-grant">创建访问令牌</button></div>
        ${CodePierConnectionSetup.tunnelHTML()}
      </details></div></section>
      <section class="panel" id="grant-panel">${grantListHTML()}</section>
      ${CodePierConnectionSetup.html()}`
  );
}
function toolCatalogHTML(tools) {
  return uiHelp(
    `工具目录 · ${tools.length} 项`,
    `<div class="project-search"><input id="tool-query" type="search" aria-label="搜索工具" placeholder="搜索工具名称或权限"><small id="tool-count" role="status"></small></div><div class="tool-grid">${tools
      .map((t) => {
        const scope =
          t.scope ||
          t._meta?.securitySchemes?.[0]?.scopes?.[0] ||
          (t.annotations?.readOnlyHint ? 'read' : 'write');
        return `<div class="tool-chip" data-tool-name="${esc((t.name + ' ' + scope).toLowerCase())}" title="${esc(t.description)}"><span>${esc(t.name)}</span><span class="tool-scope ${esc(scope)}">${esc(scope)}</span></div>`;
      })
      .join('')}</div>`,
  );
}
function permissionsHTML(
  projects,
  scopes = ['read', 'write', 'execute', 'computer'],
  defaults = S.settings?.access_defaults || {},
) {
  return (
    `<div class="field"><label>工具权限</label><div class="check-list">${scopes.map((scope) => `<label class="check"><input type="checkbox" name="scope" value="${esc(scope)}" ${scope === 'read' ? 'checked disabled' : defaults.developer_scopes && ['write', 'execute'].includes(scope) ? 'checked' : ''}>${{ read: '读取项目与文件', write: '编辑文件、恢复备份和建立检查点', execute: '运行和停止命令', computer: '读取屏幕和操作桌面应用' }[scope]}</label>`).join('')}</div></div>` +
    CodePierAccess.projectFields(
      projects,
      defaults.all_projects === true,
      defaults.selected_projects || [],
    )
  );
}
function checked(name) {
  return $$(`.modal input[name="${name}"]:checked`).map((x) => x.value);
}
async function newGrant() {
  const login = S.session,
    space = S.space_id,
    page = S.page,
    intent = S.modalIntent || 0;
  const current = () => login === S.session && space === S.space_id && page === S.page;
  await loadBasics();
  const profiles = (await api('/api/access-profiles')).profiles;
  if (!current() || intent !== (S.modalIntent || 0)) return;
  const dialog = modal(
    '创建限定范围访问凭据',
    `<form id="grant-form"><div class="form-row"><div class="field"><label>凭据名称</label><input name="label" required maxlength="80" placeholder="例如 ChatGPT Tunnel"></div><div class="field"><label>有效期限</label><select name="days"><option value="30">30 天（默认）</option><option value="7">7 天</option><option value="90">90 天</option><option value="365">365 天</option><option value="unlimited">无限期，直到手动撤销</option></select><small>无限期会持续保留访问权，请妥善保管凭据。</small></div></div>${CodePierProfiles.selectorHTML(profiles)}<div data-profile-permissions></div>${notice('适用于 Claude Code、Inspector 等支持 Bearer 的客户端。令牌只显示这一次。')}</form>`,
    buttons('create-grant', '创建凭据'),
  );
  const profileBinding = CodePierProfiles.bindSelector(
    $('.modal'),
    profiles,
    ['read', 'write', 'execute', 'computer', 'codepier.role_access'],
    S.settings?.access_defaults || {},
  );
  $('#create-grant').onclick = () =>
    busy($('#create-grant'), async () => {
      const f = $('#grant-form');
      if (!f.reportValidity()) return;
      const r = await post('/api/grants', {
        label: f.elements.label.value,
        days: f.elements.days.value === 'unlimited' ? null : Number(f.elements.days.value),
        scopes: checked('scope'),
        ...CodePierAccess.selection(),
        ...profileBinding(),
      });
      if (!current() || !dialog.isConnected) return;
      modal(
        '保存你的访问凭据',
        `${notice('持有此令牌的人可使用你的授权，请妥善保存。')}<div class="spacer"></div><div class="code-box secret">${esc(r.token)}</div><p class="form-note">期限：${r.unlimited ? '无限期' : esc(timeText(r.expires))}。需要时可随时撤销。</p>`,
        `<button class="btn ghost" data-action="close-modal">完成</button><button class="btn" id="copy-token">复制</button><button class="btn primary" id="download-token">下载 token.txt</button>`,
      );
      $('#copy-token').onclick = () => copy(r.token);
      $('#download-token').onclick = () => download('token.txt', r.token + '\n', 'text/plain');
    });
}
async function consentModal(id) {
  const login = S.session,
    space = S.space_id,
    page = S.page,
    intent = S.modalIntent || 0;
  const current = () => login === S.session && space === S.space_id && page === S.page;
  try {
    const [r, { profiles }] = await Promise.all([
      api('/api/oauth/requests/' + encodeURIComponent(id)),
      api('/api/access-profiles'),
    ]);
    if (!current() || intent !== (S.modalIntent || 0)) return;
    const roleScope = 'codepier.role_access';
    const roleOnly = r.scopes.length === 1 && r.scopes.includes(roleScope);
    const devScopes = ['read', 'write', 'execute'].filter((scope) => r.scopes.includes(scope));
    const devLabel = devScopes.includes('execute')
      ? devScopes.includes('write')
        ? '允许读写和运行'
        : '允许读取和运行'
      : '允许读写';
    const choiceKey = ['codepier-oauth-choice', sessionOwner(login), space, r.client_id]
      .map(encodeURIComponent)
      .join(':');
    let remembered = {};
    try {
      remembered = JSON.parse(localStorage.getItem(choiceKey)) || {};
    } catch {}
    const defaults = {
      all_projects: remembered.all_projects ?? r.access_defaults?.all_projects === true,
      selected_projects: Array.isArray(remembered.projects)
        ? remembered.projects
        : S.projects.map((p) => p.id),
      developer_scopes: false,
    };
    const dialog = modal(
      esc(r.client_name) + ' 想访问你的项目',
      `<p id="oauth-summary" role="status"></p>
       <details class="form-advanced" id="oauth-options" ${roleOnly ? 'open' : ''}><summary>调整期限、项目或权限</summary>
        <div class="field"><label for="oauth-days">期限</label><select id="oauth-days"><option value="30">30 天</option><option value="7">7 天</option><option value="90">90 天</option><option value="365">365 天</option><option value="unlimited">无限期</option></select></div>
        ${CodePierProfiles.selectorHTML(profiles)}<div data-profile-permissions></div>
        <details><summary>客户端信息</summary><dl class="kv"><dt>客户端</dt><dd><code>${esc(r.client_id)}</code></dd><dt>回调地址</dt><dd>${esc(r.redirect_uri)}</dd><dt>资源</dt><dd>${esc(r.resource)}</dd></dl></details>
       </details>`,
      `<button class="btn ghost" id="deny-consent">拒绝</button>
       <button class="btn ${devScopes.length > 1 ? '' : 'primary'}" id="allow-read">只允许读取</button>
       ${devScopes.length > 1 ? `<button class="btn primary" id="allow-dev">${devLabel}</button>` : ''}
       <button class="btn primary" id="allow-consent" hidden>允许所选范围</button>`,
      false,
      false,
    );
    const profileBinding = CodePierProfiles.bindSelector(dialog, profiles, r.scopes, defaults);
    const select = $('#access-profile-selector', dialog),
      options = $('#oauth-options', dialog),
      days = $('#oauth-days', dialog),
      summary = $('#oauth-summary', dialog),
      deny = $('#deny-consent', dialog),
      read = $('#allow-read', dialog),
      dev = $('#allow-dev', dialog),
      custom = $('#allow-consent', dialog);
    // Remember finite durations and project choices. Desktop and role consent
    // always require a new explicit selection.
    if (['7', '30', '90', '365'].includes(remembered.days)) days.value = remembered.days;
    const priorScopes = Array.isArray(remembered.scopes) ? remembered.scopes : [];
    if (priorScopes.length) {
      for (const input of $$('[name="scope"]:not(:disabled)', dialog))
        input.checked = input.value !== 'computer' && priorScopes.includes(input.value);
      if (dev && priorScopes.every((scope) => scope === 'read')) {
        dev.classList.remove('primary');
        read.classList.add('primary');
      }
    }
    const sync = () => {
      const profile = profiles.find((item) => item.id === select.value);
      const { all_projects, projects } = CodePierAccess.selection(dialog);
      const range =
        profile?.role_id && r.scopes.includes(roleScope)
          ? '权限会随角色变化'
          : all_projects
            ? '全部现有及未来项目'
            : `${projects.length} 个项目`;
      summary.textContent = `${days.selectedOptions[0].textContent} · ${range}。可随时在“连接”撤销。`;
      const isCustom = roleOnly || !!profile;
      read.hidden = isCustom || !r.scopes.includes('read');
      if (dev) dev.hidden = isCustom;
      custom.hidden = !isCustom && !options.open;
      custom.disabled = roleOnly && !profile;
    };
    dialog.addEventListener('change', sync);
    options.addEventListener('toggle', sync);
    sync();
    let submitting = false;
    const decide = (button, allow, preset) => {
      if (submitting || !current() || !dialog.isConnected) return;
      return busy(button, async () => {
        submitting = true;
        try {
          const scopes =
            preset || $$('input[name="scope"]:checked', dialog).map((input) => input.value);
          const selection = CodePierAccess.selection(dialog);
          const choice = allow ? profileBinding() : {};
          const out = await post(`/api/oauth/requests/${encodeURIComponent(id)}/decide`, {
            allow,
            ...(allow ? { days: days.value === 'unlimited' ? null : Number(days.value) } : {}),
            scopes,
            ...selection,
            ...choice,
          });
          if (!current() || !dialog.isConnected) return;
          if (allow && !choice.profile_id) {
            try {
              localStorage.setItem(
                choiceKey,
                JSON.stringify({
                  ...selection,
                  days: days.value,
                  scopes: scopes.filter((scope) => scope !== 'computer'),
                }),
              );
            } catch {}
          }
          location.assign(out.redirect);
        } finally {
          submitting = false;
        }
      });
    };
    deny.onclick = () => decide(deny, false);
    read.onclick = () => decide(read, true, ['read']);
    if (dev) dev.onclick = () => decide(dev, true, devScopes);
    custom.onclick = () => decide(custom, true);
  } catch (e) {
    if (!current()) return;
    toast(e.message, true);
    history.replaceState(null, '', location.pathname + location.hash);
  }
}
function accountPasswordHTML() {
  return `<section class="panel"><div class="panel-head"><h2>${icon('lock')}我的密码</h2></div><div class="panel-body"><form id="password-form"><div class="field"><label>当前密码</label><input name="current_password" type="password" required autocomplete="current-password"></div><div class="field"><label>新密码</label><input name="new_password" type="password" minlength="12" maxlength="256" required autocomplete="new-password" placeholder="至少 12 位"></div><div class="field"><label>确认新密码</label><input name="confirm_password" type="password" minlength="12" required autocomplete="new-password" aria-describedby="password-match-error"><p id="password-match-error" class="field-error" role="status" hidden></p></div><p class="form-note">仅修改你自己的密码。修改后退出全部浏览器登录，并撤销自己的 MCP 授权；已运行的操作不会自动停止。</p><button class="btn" type="submit">更新密码并退出</button></form></div></section>`;
}
function settingsHTML() {
  return CodePierSettings.html();
}
function bindSettings() {
  CodePierSettings.bind();
}
function bindPasswordForm() {
  const passwordForm = $('#password-form');
  if (!passwordForm) return;
  passwordForm.addEventListener('input', () => {
    const error = $('#password-match-error', passwordForm);
    if (!error.hidden) {
      error.hidden = true;
      error.textContent = '';
      passwordForm.elements.confirm_password.removeAttribute('aria-invalid');
    }
  });
  $('#password-form').onsubmit = (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.elements.new_password.value !== f.elements.confirm_password.value) {
      const field = f.elements.confirm_password;
      field.setAttribute('aria-invalid', 'true');
      const error = $('#password-match-error', f);
      error.hidden = false;
      error.textContent = '两次新密码不一致';
      field.focus();
      return;
    }
    busy($('button', f), async () => {
      await post('/api/account/password', {
        current_password: f.elements.current_password.value,
        new_password: f.elements.new_password.value,
      });
      endSession(true);
      toast('密码已更新，请重新登录并重新建立 MCP 授权');
    });
  };
}
async function busy(button, fn) {
  return CP.ui.busy(button, fn, toast);
}
const panelActions = CP.actions.create();
panelActions.register(['grant-access-preview'], async (button) => {
  await CodePierProfiles.preview(button.dataset.id);
});
panelActions.register(['close-modal'], async (b, e) => {
  await panelDialogs.requestClose();
  return;
});
panelActions.register(['toggle-menu'], async (b, e) => {
  uiSetMenu(undefined, true, b);
  return;
});
panelActions.register(['logout'], async (b, e) => {
  if (
    hasUnsavedChanges() &&
    !(await uiConfirmAction(
      {
        title: '退出登录？',
        body: '退出会丢弃当前标签页的未保存草稿、附件和待确认请求。',
        confirmLabel: '退出登录',
        danger: true,
      },
      { owner: b },
    ))
  )
    return;
  await post('/api/logout');
  endSession(true);
  return;
});
panelActions.register(['refresh'], async (b, e) => {
  await renderPage();
  return;
});
panelActions.register(['add-device'], async (b, e) => {
  await addDevice();
  return;
});
panelActions.register(['agent-roots'], async (b) => {
  await CodePierRoots.open(b.dataset.id);
});
panelActions.register(['device-detail'], async (b, e) => {
  await deviceDetail(b.dataset.id);
  return;
});
panelActions.register(['agent-update', 'agent-restart', 'agent-uninstall'], async (b, e) => {
  {
    let device = S.devices.find((item) => item.id === b.dataset.id);
    if (!device) {
      await loadBasics();
      device = S.devices.find((item) => item.id === b.dataset.id);
    }
    if (!device) throw new Error('电脑信息已变化，请刷新后重试');
    agentLifecycleModal(device, b.dataset.action.replace('-', '_'));
    return;
  }
});
panelActions.register(['agent-commands'], async (b, e) => {
  {
    let device = S.devices.find((item) => item.id === b.dataset.id);
    if (!device) {
      await loadBasics();
      device = S.devices.find((item) => item.id === b.dataset.id);
    }
    if (!device) throw new Error('电脑信息已变化，请刷新后重试');
    showAgentMaintenanceCommands(device);
    return;
  }
});
panelActions.register(['agent-repair'], async (b, e) => {
  {
    let device = S.devices.find((item) => item.id === b.dataset.id);
    if (!device) {
      await loadBasics();
      device = S.devices.find((item) => item.id === b.dataset.id);
    }
    if (!device) throw new Error('电脑信息已变化，请刷新后重试');
    await agentInstallSetup(device);
    return;
  }
});
panelActions.register(['add-project'], async (b, e) => {
  await projectModal(null, b.dataset.device);
  return;
});
panelActions.register(['edit-project'], async (b, e) => {
  await projectModal(b.dataset.id);
  return;
});
panelActions.register(['project-collaboration'], async (b) => {
  await window.CodePierAssets.load('collaboration');
  if (!S.session) return;
  await CodePierCollaboration.open(b.dataset.id);
});
panelActions.register(['open-project'], async (b, e) => {
  if (
    S.work.dirty &&
    !(await uiConfirmAction(
      {
        title: '打开项目？',
        body: '打开项目将丢弃当前未保存草稿。',
        confirmLabel: '放弃草稿并打开',
        danger: true,
      },
      { owner: b },
    ))
  )
    return;
  resetWork(b.dataset.id);
  await navigate('workbench');
  return;
});
panelActions.register(['tree-refresh'], async (b, e) => {
  await loadTree();
  return;
});
panelActions.register(['tree-home'], async (b, e) => {
  S.work.treePath = '.';
  await loadTree();
  return;
});
panelActions.register(['tree-dir'], async (b, e) => {
  S.work.treePath = b.dataset.path;
  await loadTree();
  return;
});
panelActions.register(['tree-more'], async (b, e) => {
  await loadTree(true);
  return;
});
panelActions.register(['read-file'], async (b, e) => {
  await readFile(b.dataset.path);
  return;
});
panelActions.register(['search-read'], async (b, e) => {
  if (!b.isConnected || !workDialogCurrent(b.workScope)) return;
  closeModal(b.closest('.modal'));
  await readFile(b.dataset.path);
  return;
});
panelActions.register(['new-file'], async (b, e) => {
  await newFile();
  return;
});
panelActions.register(['reload-file'], async (b, e) => {
  await readFile(S.work.path);
  return;
});
panelActions.register(['preview-save'], async (b, e) => {
  await previewSave();
  return;
});
panelActions.register(['delete-file'], async (b, e) => {
  await deleteFile();
  return;
});
panelActions.register(['move-file'], async (b, e) => {
  await moveFile();
  return;
});
panelActions.register(['history'], async (b, e) => {
  await historyModal();
  return;
});
panelActions.register(['restore-backup'], async (b, e) => {
  if (!b.isConnected) return;
  await restoreBackup(b.dataset.id, b.dataset.path, b.workScope);
  return;
});
panelActions.register(['checkpoint'], async (b, e) => {
  await checkpoint();
  return;
});
panelActions.register(['git-status'], async (b, e) => {
  await gitView('git_status');
  return;
});
panelActions.register(['git-diff'], async (b, e) => {
  await gitView('git_diff');
  return;
});
panelActions.register(['load-tasks'], async (b, e) => {
  await loadTasks();
  return;
});
panelActions.register(['run-task'], async (b, e) => {
  await runTask();
  return;
});
panelActions.register(['cancel-task'], async (b, e) => {
  if (S.work.operation) {
    await tool('operations_cancel', { operation_id: S.work.operation });
    toast('已发送停止请求，请核对任务结果');
  }
  return;
});
panelActions.register(['operation-detail'], async (b, e) => {
  await operationDetail(b.dataset.id);
  return;
});
panelActions.register(['audit-mode'], async (b, e) => {
  window.CodePierCallLog?.clear();
  S.auditMode = b.dataset.mode;
  if (S.auditMode === 'operations') S.auditQuery = S.auditQuery.slice(0, 200);
  S.auditOffset = 0;
  S.auditStatus = '';
  S.auditSource = '';
  await renderPage(false);
  return;
});
panelActions.register(['audit-search'], async (b, e) => {
  S.auditQuery = $('#audit-query').value;
  S.auditOffset = 0;
  await renderPage(false);
  return;
});
panelActions.register(['audit-prev'], async (b, e) => {
  S.auditOffset = Math.max(0, S.auditOffset - 40);
  await renderPage(false);
  return;
});
panelActions.register(['audit-next'], async (b, e) => {
  if (S.auditNext !== null) {
    S.auditOffset = S.auditNext;
    await renderPage(false);
  }
  return;
});
panelActions.register(['export-audit'], async (b, e) => {
  {
    if (!S.space_id) throw new Error('请先选择仍可访问的工作空间。');
    const url =
      '/api/audit-export?' +
      new URLSearchParams({
        space_id: S.space_id || '',
        source: S.auditSource,
        q: S.auditQuery,
        offset: '0',
      });
    const a = document.createElement('a');
    a.href = url;
    a.download = 'codepier-audit.csv';
    a.click();
    toast('导出全部事件中匹配来源/关键词的前 10,000 条；不是仅工具执行列表。');
    return;
  }
});
panelActions.register(['copy-endpoint'], async (b, e) => {
  await copy(b.dataset.url || S.settings.mcp_url);
  return;
});
panelActions.register(['copy-coding-endpoint'], async (b, e) => {
  await copy(codingEndpoint());
  return;
});
panelActions.register(['new-grant'], async (b, e) => {
  await newGrant();
  return;
});
panelActions.register(['grant-history'], (button) => {
  S.grantHistoryOpen = !S.grantHistoryOpen;
  refreshGrantList(button);
});
panelActions.register(['grant-page'], (button) => {
  const group = button.dataset.group;
  if (!['current', 'history'].includes(group)) return;
  S.grantPages[group] += Number(button.dataset.delta);
  refreshGrantList(button);
});
panelActions.register(['edit-grant'], async (button) => {
  await CodePierAccess.editGrant(button.dataset.id);
});
panelActions.register(['revoke-grant'], async (b, e) => {
  if (
    await uiConfirmAction(
      {
        title: '撤销连接？',
        body: '撤销后，客户端将失去访问权限。已启动的任务可能继续执行。',
        confirmLabel: '撤销权限',
        danger: true,
      },
      { owner: b },
    )
  ) {
    await api('/api/grants/' + b.dataset.id, { method: 'DELETE' });
    await renderPage(false);
  }
  return;
});
panelActions.register(['retry-identity'], async () => {
  await bootAuthenticated();
});
panelActions.register(['copy-role-endpoint'], async () => {
  await copy(S.settings.role_mcp_url);
});
document.addEventListener('click', async (e) => {
  try {
    const copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      await copy(copyBtn.dataset.copy);
      return;
    }
    const navButton = e.target.closest('[data-nav]');
    if (navButton) {
      await navigate(navButton.dataset.nav);
      return;
    }
    const button = e.target.closest('[data-action]');
    if (button && !button.disabled) await panelActions.dispatch(button.dataset.action, button, e);
  } catch (error) {
    toast(error.message, true);
  }
});
window.addEventListener('beforeunload', (e) => {
  if (hasUnsavedChanges()) {
    e.preventDefault();
    e.returnValue = '';
  }
});
window.addEventListener('hashchange', () => {
  const page = location.hash.slice(1) === 'terminal' ? 'native' : location.hash.slice(1);
  if (page !== S.page && nav.some((n) => n[0] === page)) navigate(page);
});
(async () => {
  try {
    const page = location.hash.slice(1) === 'terminal' ? 'native' : location.hash.slice(1);
    if (nav.some((n) => n[0] === page)) S.page = page;
    if (location.hash === '#terminal')
      history.replaceState(null, '', location.pathname + location.search + '#native');
    const s = await api('/api/session');
    if (s.authenticated) {
      S.session = s;
      await bootAuthenticated();
    } else renderLogin(s.configured);
  } catch (e) {
    $('#app').innerHTML =
      `<div class="boot">${brand}<p>${esc(e.message)}</p><button class="btn" id="retry-boot">重新连接</button></div>`;
    $('#retry-boot').onclick = () => location.reload();
  }
})();
