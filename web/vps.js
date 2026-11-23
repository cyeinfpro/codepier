'use strict';
// Saved connection management. Passwords stay in the active form only; neither
// metadata state nor browser storage ever receives a saved credential.
const VPS_TERMINAL_ENDED = ['closed', 'exited', 'failed', 'revoked'];
async function vpsInventory() {
  const items = [];
  let offset = 0;
  do {
    const r = await api('/api/vps?limit=200&offset=' + offset);
    items.push(...r.vps);
    offset = r.next_offset;
  } while (offset !== null);
  return items;
}
function vpsIntent() {
  const session = S.session,
    page = S.page,
    space = S.space_id,
    id = (S.vpsIntent = (S.vpsIntent || 0) + 1);
  return () =>
    session === S.session && page === S.page && space === S.space_id && id === S.vpsIntent;
}
function vpsEndpoint(v) {
  return `${v.host.includes(':') ? '[' + v.host + ']' : v.host}:${v.port}`;
}
function vpsShellQuote(value) {
  const text = String(value || '');
  return /^[A-Za-z0-9_@.:%/+-]+$/.test(text) ? text : `'${text.replace(/'/g, "'\\''")}'`;
}
function vpsSshCommand(v) {
  const host = String(v.host || '')
    .trim()
    .replace(/^\[|\]$/g, '');
  const user = String(v.username || 'root').trim() || 'root';
  const port = Number(v.port) || 22;
  const ipv6 = host.includes(':') ? ' -6' : '';
  const portArg = port === 22 ? '' : ` -p ${port}`;
  const target = vpsShellQuote(`${user}@${host}`);
  const sep = target.startsWith('-') ? ' --' : '';
  return `ssh${ipv6}${portArg}${sep} ${target}`;
}
function vpsLayout() {
  if (S.vpsLayout === 'list' || S.vpsLayout === 'cards') return S.vpsLayout;
  if (typeof localStorage !== 'undefined') {
    try {
      const saved = localStorage.getItem('codepier-vps-layout');
      if (saved === 'list' || saved === 'cards') return (S.vpsLayout = saved);
    } catch {}
  }
  return 'cards';
}
let vpsTerminalOwner = null;
function vpsTerminalIdentity() {
  if (!S.session?.user_id || !S.space_id) return null;
  return JSON.stringify([S.session.user_id, S.space_id]);
}
function vpsTerminalForget() {
  vpsTerminalOwner = null;
}
function vpsActiveSessions(vpsId = null) {
  if (typeof window === 'undefined') return [];
  const items = window.CodePierVpsTerminal?.list?.() || [];
  return items.filter(
    (item) => item && !VPS_TERMINAL_ENDED.includes(item.state) && (!vpsId || item.vps_id === vpsId),
  );
}
function vpsTerminalCount() {
  if (typeof window === 'undefined') return 0;
  return window.CodePierVpsTerminal?.count?.() || vpsActiveSessions().length;
}
function vpsTerminalNavigate(id, { terminal = false } = {}) {
  if (!S.session || S.page !== 'vps') return;
  const destination = id
    ? 'vps?view=terminal&session=' + encodeURIComponent(id)
    : terminal
      ? 'vps?view=terminal'
      : 'vps';
  if (location.hash !== '#' + destination) void navigate(destination);
}
function vpsActiveSessionsInnerHTML(sessions = vpsActiveSessions(), total = vpsTerminalCount()) {
  const count = total || sessions.length;
  if (!count) return '';
  const pills = sessions
    .map((item) => {
      const vps = S.vps?.find((x) => x.id === item.vps_id);
      const name = item.vps_name || item.title || vps?.name || 'SSH 终端';
      const hostPort =
        item.endpoint ||
        (vps ? vpsEndpoint(vps) : item.host ? `${item.host}:${item.port || 22}` : '');
      const user = item.username || vps?.username || '';
      const target = user && hostPort ? `${user}@${hostPort}` : hostPort;
      const sid = item.session_id || item.id || '';
      const tmux =
        item.tmux !== false && item.persistence !== 'none' && item.persistence !== 'ephemeral';
      return `<button type="button" class="btn ghost small vps-session-pill" data-vps-action="resume-session" data-session="${esc(sid)}" data-id="${esc(item.vps_id || '')}"><span class="dot online" aria-hidden="true"></span><strong>${esc(name)}</strong>${target ? `<code>${esc(target)}</code>` : ''}${tmux ? '<span class="badge neutral">tmux 持久</span>' : ''}</button>`;
    })
    .join('');
  return `<div class="vps-active-head"><span class="dot online" aria-hidden="true"></span><strong>活跃终端 · ${count}</strong></div>${pills ? `<div class="vps-session-list">${pills}</div>` : ''}<button type="button" class="btn small vps-sessions-enter" data-vps-action="sessions">${icon('terminal')}进入终端工作台</button>`;
}
function vpsSyncTerminalPresence() {
  const sessions = vpsActiveSessions(),
    count = vpsTerminalCount();
  $$('[data-vps-terminal-count]').forEach((node) => {
    node.textContent = count ? `终端会话 · ${count}` : '终端会话';
  });
  const statNode = $('[data-vps-stat="sessions"]');
  if (statNode) statNode.textContent = String(count);
  const bar = $('.vps-active-sessions');
  if (bar) {
    if (count > 0) {
      bar.hidden = false;
      bar.innerHTML = vpsActiveSessionsInnerHTML(sessions, count);
    } else {
      bar.hidden = true;
      bar.replaceChildren();
    }
  }
  $$('[data-vps-card]').forEach((card) => {
    const id = card.dataset.vpsCard,
      cardCount = sessions.filter((item) => item.vps_id === id).length,
      v = S.vps?.find((item) => item.id === id);
    const badge = card.querySelector('[data-vps-card-sessions]');
    if (badge) {
      badge.hidden = !cardCount;
      badge.textContent = cardCount ? `终端 · ${cardCount}` : '';
    }
    const termBtn = card.querySelector('[data-vps-action="terminal"]');
    if (termBtn) {
      termBtn.innerHTML = `${icon('terminal')}${cardCount ? `返回终端 · ${cardCount}` : '打开终端'}`;
    }
    const cta = card.querySelector('.vps-footer-cta');
    let newBtn = card.querySelector('[data-vps-action="terminal-new"]');
    const canNew = cardCount > 0 && termBtn && !termBtn.disabled;
    if (canNew && !newBtn && cta) {
      newBtn = document.createElement('button');
      newBtn.type = 'button';
      newBtn.className = 'btn ghost small vps-terminal-new';
      newBtn.dataset.vpsAction = 'terminal-new';
      newBtn.dataset.id = id;
      newBtn.title = '新开终端会话';
      newBtn.setAttribute('aria-label', `为 ${v?.name || 'VPS'} 新开终端`);
      newBtn.innerHTML = icon('plus');
      cta.append(newBtn);
    } else if (!canNew && newBtn) {
      newBtn.remove();
    }
  });
  if ($('#vps-status-filter')?.value === 'active') vpsFilter();
}
async function vpsTerminalModule() {
  const owner = vpsTerminalIdentity();
  if (!owner) throw new Error('请先登录并选择空间');
  await window.CodePierAssets.load('vps-terminal');
  if (owner !== vpsTerminalIdentity()) throw sessionChanged();
  vpsTerminalOwner = owner;
  const terminal = window.CodePierVpsTerminal;
  if (!terminal) throw new Error('终端资源未能加载，请重试');
  terminal.configure({
    api,
    getIdentity: vpsTerminalIdentity,
    onNavigate: vpsTerminalNavigate,
    onCountChange: () => vpsSyncTerminalPresence(),
    onEditVps: (id) => vpsEdit(id),
  });
  return terminal;
}
async function vpsTerminalPage(seq) {
  const owner = vpsTerminalIdentity(),
    query = new URLSearchParams(parseHashRoute().query),
    current = () => seq === S.renderSeq && S.page === 'vps' && owner === vpsTerminalIdentity();
  const terminal = await vpsTerminalModule();
  if (!current()) return;
  const page = $('#page');
  let host = $('#vps-terminal-host', page);
  if (!host) {
    terminal.hide();
    host = document.createElement('div');
    host.id = 'vps-terminal-host';
    page.replaceChildren(host);
  }
  uiPageReady(false);
  await terminal.mount(host, { sessionId: query.get('session') || undefined });
}
function vpsTerminalProjects(v) {
  return (v.projects || [])
    .filter((p) => p.mode === 'write' && p.allow_tasks && p.enabled !== false)
    .sort((a, b) => Number(!!b.online) - Number(!!a.online));
}
function vpsTerminalReason(v) {
  if (!v.enabled) return '连接已停用，启用后可打开终端。';
  if (v.terminal && typeof v.terminal === 'object') {
    if (v.terminal.eligible === false) {
      const code = v.terminal.reason_code || v.terminal.code || '';
      if (code === 'SSH_TERMINAL_FORBIDDEN' || code === 'FORBIDDEN')
        return '连接项目需要写入和执行命令权限。';
      if (code === 'NO_TERMINAL_AGENT' || code === 'AGENT_OFFLINE' || code === 'NODE_OFFLINE')
        return '当前空间暂无可用电脑，请先添加电脑。';
      if (code === 'SHELL_DISABLED') return '连接电脑尚未开启命令执行权限。';
      return v.terminal.reason || '当前无法打开终端，请检查连接电脑状态。';
    }
    return '';
  }
  if ((v.project_ids || []).length > 0 && vpsTerminalProjects(v).length === 0) {
    return '连接项目需要写入和执行命令权限。';
  }
  return '';
}
function vpsChannelLabel(v) {
  if (!v.enabled) return '已停用';
  if (v.terminal?.device_name) return `经由 ${v.terminal.device_name} 直连`;
  const projects = vpsTerminalProjects(v);
  const online = projects.find((p) => p.online);
  if (online?.device_name) return `经由 ${online.device_name} 直连`;
  if (projects.length) return '等待连接电脑上线';
  if (v.terminal?.eligible === true) return '空间电脑直连就绪';
  if (!v.terminal && !(v.project_ids || []).length) return '空间电脑直连';
  return '待授权执行权限';
}
function vpsActionsHTML(v) {
  const projects = vpsTerminalProjects(v),
    reason = vpsTerminalReason(v),
    checkBlocked = !v.enabled || (!!reason && !projects.length),
    checkTitle = !v.enabled ? '已停用，启用后可检查' : checkBlocked ? reason : '',
    count = vpsActiveSessions(v.id).length,
    terminalCode = v.terminal?.reason_code || v.terminal?.code || '',
    showProjectFix =
      v.enabled &&
      (v.project_ids || []).length > 0 &&
      !projects.length &&
      reason === '连接项目需要写入和执行命令权限。',
    showDeviceFix =
      v.enabled &&
      ['NO_TERMINAL_AGENT', 'AGENT_OFFLINE', 'NODE_OFFLINE', 'SHELL_DISABLED'].includes(
        terminalCode,
      );
  return `<footer><button class="btn ghost small" type="button" data-vps-action="edit" data-id="${esc(v.id)}" aria-label="编辑 ${esc(v.name)}">${icon('edit')}编辑</button><button class="btn ghost small" type="button" data-vps-action="check" data-id="${esc(v.id)}" ${checkBlocked ? `disabled title="${esc(checkTitle)}"` : ''}>检查连接</button><div class="vps-footer-cta"><button class="btn primary small" type="button" data-vps-action="terminal" data-id="${esc(v.id)}" ${reason ? `disabled aria-describedby="vps-terminal-hint-${esc(v.id)}"` : ''}>${icon('terminal')}${count ? `返回终端 · ${count}` : '打开终端'}</button>${count && !reason ? `<button class="btn ghost small vps-terminal-new" type="button" data-vps-action="terminal-new" data-id="${esc(v.id)}" title="新开终端会话" aria-label="为 ${esc(v.name)} 新开终端">${icon('plus')}</button>` : ''}</div></footer>${reason ? `<p class="vps-terminal-hint" id="vps-terminal-hint-${esc(v.id)}">${esc(reason)}${showProjectFix ? `<button class="btn ghost small" type="button" data-action="edit-project" data-id="${esc(v.project_ids[0])}">项目权限</button>` : showDeviceFix ? '<button class="btn ghost small" type="button" data-nav="devices">电脑管理</button>' : ''}</p>` : ''}<div class="vps-terminal-inline" data-vps-terminal-inline hidden></div>`;
}
function vpsTerminalInline(id) {
  const card = $$('[data-vps-card]').find((node) => node.dataset.vpsCard === id);
  return card?.querySelector('[data-vps-terminal-inline]') || null;
}
function vpsTerminalMessage(id, message, { error = false, action } = {}) {
  const root = vpsTerminalInline(id);
  if (!root) return;
  root.hidden = false;
  root.replaceChildren();
  const text = document.createElement('p');
  text.textContent = message;
  text.setAttribute('role', error ? 'alert' : 'status');
  root.append(text);
  if (action) {
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'btn small';
    link.textContent = action.label;
    link.addEventListener('click', action.run);
    root.append(link);
  }
}
async function vpsOpenTerminal(id, button, { newSession = false } = {}) {
  if (!newSession) {
    const activeSessions = vpsActiveSessions(id);
    if (activeSessions.length) {
      const sid = activeSessions[0].session_id || activeSessions[0].id;
      await navigate(
        sid ? 'vps?view=terminal&session=' + encodeURIComponent(sid) : 'vps?view=terminal',
      );
      return;
    }
  }
  const current = vpsIntent();
  if (button) {
    button.disabled = true;
    button.dataset.vpsOpening = 'true';
  }
  try {
    const v = await api('/api/vps/' + encodeURIComponent(id));
    if (!current()) return;
    if (!v.enabled) throw new Error('连接已停用，请先编辑并启用 VPS');
    const via = v.terminal?.device_name;
    vpsTerminalMessage(id, via ? `正在经由 ${via} 连接…` : `正在连接 ${v.name}…`);
    const terminal = await vpsTerminalModule();
    if (!current()) return;
    await terminal.open({ vps: v, newSession, isCurrent: current });
  } catch (error) {
    if (!current()) return;
    const code = error.code || error.detail?.code || error.reason_code;
    const upgrade = [
        'AGENT_UPGRADE_REQUIRED',
        'PROTOCOL_MISMATCH',
        'SSH_DEPENDENCY_MISSING',
      ].includes(code),
      offline = ['AGENT_OFFLINE', 'NODE_OFFLINE', 'NO_TERMINAL_AGENT'].includes(code),
      shellDisabled = code === 'SHELL_DISABLED',
      forbidden = ['FORBIDDEN', 'SSH_TERMINAL_FORBIDDEN'].includes(code);
    const message = upgrade
      ? '连接电脑需要更新才能使用 Web SSH。前往电脑管理，更新后重试。'
      : code === 'NO_TERMINAL_AGENT'
        ? '当前空间暂无可用电脑，请先添加电脑或等待电脑上线。'
        : offline
          ? '连接电脑暂时离线。电脑恢复在线后可以重试。'
          : shellDisabled
            ? '连接电脑尚未开启命令执行权限，请前往电脑管理开启后重试。'
            : forbidden
              ? '你没有此服务器的终端执行权限，请联系空间管理员。'
              : error.message || '终端暂时无法连接，请重试';
    if (vpsTerminalInline(id)) {
      vpsTerminalMessage(id, message, {
        error: true,
        action:
          upgrade || offline || shellDisabled
            ? { label: '查看连接电脑', run: () => navigate('devices') }
            : undefined,
      });
    } else if (typeof toast === 'function') {
      toast(message, true);
    }
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.removeAttribute('data-vps-opening');
    }
  }
}
function vpsReplacePage(html) {
  // The workspace lives outside the replaceable inventory, even if this
  // function is called directly by a background refresh.
  window.CodePierVpsTerminal?.hide();
  const page = $('#page'),
    next = document.createElement('template');
  next.innerHTML = html;
  const grid = $('.vps-grid', page),
    freshGrid = $('.vps-grid', next.content);
  if (!grid || !freshGrid) {
    page.replaceChildren(next.content);
    return;
  }
  grid.dataset.layout = freshGrid.dataset.layout || vpsLayout();
  const existing = new Map($$('[data-vps-card]', grid).map((card) => [card.dataset.vpsCard, card]));
  let cursor = grid.firstElementChild;
  for (const card of [...freshGrid.children]) {
    const previous = existing.get(card.dataset.vpsCard);
    const node =
      previous && vpsComparableCard(previous) === vpsComparableCard(card) ? previous : card;
    if (node === cursor) cursor = cursor.nextElementSibling;
    else if (cursor?.dataset.vpsCard === card.dataset.vpsCard) {
      const following = cursor.nextElementSibling;
      cursor.replaceWith(node);
      cursor = following;
    } else grid.insertBefore(node, cursor);
  }
  while (cursor) {
    const following = cursor.nextElementSibling;
    cursor.remove();
    cursor = following;
  }
  const nodes = [...next.content.childNodes],
    index = nodes.indexOf(freshGrid);
  for (const node of [...page.childNodes]) if (node !== grid) node.remove();
  grid.before(...nodes.slice(0, index));
  grid.after(...nodes.slice(index + 1));
}
function vpsComparableCard(card) {
  const copy = card.cloneNode(true),
    inline = copy.querySelector('[data-vps-terminal-inline]');
  // Metadata refreshes must not wipe an open connection status or error alert.
  if (inline) {
    inline.hidden = true;
    inline.replaceChildren();
  }
  for (const button of copy.querySelectorAll('[data-vps-opening]')) {
    button.disabled = false;
    button.removeAttribute('data-vps-opening');
  }
  copy.hidden = false;
  return copy.outerHTML;
}
function vpsCardHTML(v, sessions) {
  const activeCount = sessions.filter((item) => item.vps_id === v.id).length,
    ready = !vpsTerminalReason(v),
    subtitle = [v.provider, v.region].filter(Boolean).join(' · ') || '通用 SSH 服务器',
    projects = v.projects || [];
  return `<article class="panel vps-card" data-vps-card="${esc(v.id)}"><header><span class="vps-symbol">${icon('cloud')}</span><div class="vps-title-block"><h2>${esc(v.name)}</h2><p>${esc(subtitle)}</p></div><div class="vps-badges"><span class="badge running" data-vps-card-sessions ${activeCount ? '' : 'hidden'}>${activeCount ? `终端 · ${activeCount}` : ''}</span><span class="badge ${!v.enabled ? 'offline' : 'neutral'}">${!v.enabled ? '已停用' : ready ? '<span class="dot online" aria-hidden="true"></span>就绪' : '待授权'}</span></div></header><div class="vps-address"><code>${esc(v.username)}@${esc(vpsEndpoint(v))}</code><div class="vps-address-actions"><button class="btn ghost small" type="button" data-vps-action="copy-ssh" data-id="${esc(v.id)}" title="复制 SSH 命令" aria-label="复制 ${esc(v.name)} 的 SSH 命令">${icon('terminal')}SSH</button><button class="icon-btn" type="button" data-vps-action="copy" data-id="${esc(v.id)}" title="复制连接地址" aria-label="复制 ${esc(v.name)} 的地址">${icon('copy')}</button></div></div><dl class="vps-facts"><div><dt>系统环境</dt><dd>${esc(v.system || '未填写')}</dd></div><div class="vps-fact-secondary"><dt>密钥策略</dt><dd>${v.host_key_policy === 'strict' ? '严格验证' : '首次信任'}</dd></div><div class="vps-fact-secondary"><dt>连接通道</dt><dd>${esc(vpsChannelLabel(v))}</dd></div><div class="vps-fact-projects"><dt>AI 关联项目</dt><dd><div class="vps-projects" aria-label="已分配项目">${projects.map((p) => `<span class="badge neutral">${icon('folder')}${esc(p.alias)}</span>`).join('') || '<span class="muted tiny">未关联（仅人工终端）</span>'}</div></dd></div></dl>${v.notes ? `<p class="vps-notes">${esc(v.notes)}</p>` : ''}${vpsActionsHTML(v)}</article>`;
}
async function vpsHTML(seq) {
  const [items] = await Promise.all([vpsInventory(), loadBasics()]);
  if (seq !== S.renderSeq) return '';
  S.vps = items;
  const sessions = vpsActiveSessions(),
    activeTotal = vpsTerminalCount(),
    readyCount = items.filter((v) => !vpsTerminalReason(v)).length,
    linkedProjectCount = new Set(items.flatMap((v) => v.project_ids || [])).size,
    layout = vpsLayout(),
    statusFilter = S.vpsStatus || '';
  return (
    heading(
      'VPS',
      '',
      '统一管理 SSH 服务器凭据、直连 Web 终端与 AI 项目授权',
      `<button class="btn" type="button" data-vps-action="sessions">${icon('terminal')}<span data-vps-terminal-count>终端会话${activeTotal ? ' · ' + activeTotal : ''}</span></button><button class="btn primary" type="button" data-vps-action="add">${icon('plus')}添加 VPS</button>`,
    ) +
    `<section class="panel vps-summary" aria-label="服务器概览"><div class="vps-stat"><strong data-vps-stat="total">${items.length}</strong><span>服务器总数</span></div><div class="vps-stat"><strong data-vps-stat="ready">${readyCount}</strong><span>可直连就绪</span></div><div class="vps-stat"><strong data-vps-stat="sessions">${activeTotal}</strong><span>活跃终端</span></div><div class="vps-stat"><strong data-vps-stat="projects">${linkedProjectCount}</strong><span>已关联项目</span></div></section>
    <section class="panel vps-active-sessions" aria-label="活跃终端会话" ${activeTotal ? '' : 'hidden'}>${activeTotal ? vpsActiveSessionsInnerHTML(sessions, activeTotal) : ''}</section>
    <div class="vps-filters"><label class="vps-search">${icon('search')}<input id="vps-query" type="search" placeholder="搜索名称、IP、账号、服务商、地区、系统或备注" aria-label="搜索 VPS" value="${esc(S.vpsQuery || '')}"></label><select id="vps-project-filter" aria-label="按项目筛选"><option value="">全部项目</option><option value="__unassigned__" ${S.vpsProject === '__unassigned__' ? 'selected' : ''}>未分配</option>${S.projects.map((p) => `<option value="${esc(p.id)}" ${S.vpsProject === p.id ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select><select id="vps-status-filter" aria-label="按状态筛选"><option value="">全部状态</option><option value="ready" ${statusFilter === 'ready' ? 'selected' : ''}>可直连</option><option value="active" ${statusFilter === 'active' ? 'selected' : ''}>活跃终端</option><option value="disabled" ${statusFilter === 'disabled' ? 'selected' : ''}>已停用</option></select><span class="muted tiny" id="vps-count" role="status"></span><div class="tabs vps-layout-tabs" role="group" aria-label="显示方式"><button type="button" class="${layout === 'cards' ? 'active' : ''}" data-vps-layout="cards" aria-pressed="${layout === 'cards'}">卡片</button><button type="button" class="${layout === 'list' ? 'active' : ''}" data-vps-layout="list" aria-pressed="${layout === 'list'}">列表</button></div></div>
    <section class="vps-grid" data-layout="${esc(layout)}">${items.map((v) => vpsCardHTML(v, sessions)).join('')}</section>
    <div id="vps-empty" class="empty panel" hidden><p>${items.length ? '没有匹配的 VPS' : '还没有保存服务器连接'}</p>${items.length ? '' : '<button class="btn primary" type="button" data-vps-action="add">添加第一台 VPS</button>'}</div>
    <p class="form-note">添加服务器即可直接打开 Web 终端；勾选关联项目后，也可以在对话中让 AI 连接服务器排查。</p>`
  );
}
function vpsFilter() {
  const q = ($('#vps-query')?.value || '').trim().toLocaleLowerCase(),
    project = $('#vps-project-filter')?.value || '',
    status = $('#vps-status-filter')?.value || '',
    sessions = vpsActiveSessions();
  S.vpsQuery = $('#vps-query')?.value || '';
  S.vpsProject = project;
  S.vpsStatus = status;
  let count = 0,
    visibleAssigned = 0;
  $$('[data-vps-card]').forEach((card) => {
    const v = S.vps.find((item) => item.id === card.dataset.vpsCard);
    if (!v) return;
    const searchText = [v.name, v.host, v.username, v.provider, v.region, v.system, v.notes]
      .filter(Boolean)
      .join(' ')
      .toLocaleLowerCase();
    const statusMatch =
      !status ||
      (status === 'ready'
        ? !vpsTerminalReason(v)
        : status === 'active'
          ? sessions.some((item) => item.vps_id === v.id)
          : status === 'disabled'
            ? !v.enabled
            : true);
    const match =
      (!q || searchText.includes(q)) &&
      (!project ||
        (project === '__unassigned__' ? !v.project_ids.length : v.project_ids.includes(project))) &&
      statusMatch;
    card.hidden = !match;
    if (match) {
      count++;
      if (v.project_ids.length) visibleAssigned++;
    }
  });
  if ($('#vps-count')) $('#vps-count').textContent = `${count} 台 · ${visibleAssigned} 台已关联`;
  if ($('#vps-empty')) $('#vps-empty').hidden = count > 0;
}
function bindVps() {
  if ($('#vps-query')) $('#vps-query').oninput = vpsFilter;
  if ($('#vps-project-filter')) $('#vps-project-filter').onchange = vpsFilter;
  if ($('#vps-status-filter')) $('#vps-status-filter').onchange = vpsFilter;
  vpsFilter();
}
function vpsProjectChoices(selected = []) {
  return `<div class="check-list vps-check-list">${
    S.projects
      .map(
        (
          p,
        ) => `<div class="vps-project-choice"><label class="check"><input type="checkbox" name="project_ids" value="${esc(p.id)}" ${selected.includes(p.id) ? 'checked' : ''}><span>${esc(p.alias)}<small>${esc(p.device_name)} · ${p.online ? '在线' : '离线'}</small></span></label>
    ${p.mode !== 'write' || !p.allow_tasks ? `<small data-vps-permission-hint ${selected.includes(p.id) ? '' : 'hidden'}>此项目还不能运行命令。 <button class="btn ghost small" type="button" data-action="edit-project" data-id="${esc(p.id)}">去开启</button></small>` : ''}</div>`,
      )
      .join('') || '<p>暂无项目，可以先保存 VPS 并直接使用终端。</p>'
  }</div>`;
}
async function vpsEdit(id = null) {
  const current = vpsIntent();
  const [v] = await Promise.all([id ? api('/api/vps/' + id) : Promise.resolve({}), loadBasics()]);
  if (!current()) return;
  const policy = v.host_key_policy || (id ? 'strict' : 'accept-new');
  const dialog = modal(
    id ? '编辑 VPS · ' + v.name : '添加 VPS',
    `<form id="vps-form" autocomplete="off"><div class="vps-form-grid"><section class="form-section vps-form-main"><div class="vps-section-head"><h3>SSH 连接凭据</h3><p class="form-note">密码在服务端加密存储，保存后即可一键打开浏览器终端。</p></div><div class="field"><label>VPS 名称</label><input name="name" required maxlength="80" value="${esc(v.name || '')}" placeholder="例如：香港面板、广州轻量"></div><div class="form-row"><div class="field"><label>IP / 主机名</label><input name="host" required maxlength="253" value="${esc(v.host || '')}" placeholder="服务器 IP 或域名" autocapitalize="off" spellcheck="false"></div><div class="field vps-port"><label>SSH 端口</label><input name="port" type="number" required min="1" max="65535" value="${v.port || 22}"></div></div><div class="form-row"><div class="field"><label>SSH 账号</label><input name="username" required maxlength="128" value="${esc(v.username || 'root')}" autocapitalize="off" spellcheck="false"></div><div class="field"><label>SSH 密码</label><input name="password" type="password" ${id ? '' : 'required'} maxlength="4096" autocomplete="new-password" placeholder="${id ? '已保存，留空保持原密码' : '仅用于连接服务器'}"><small>${id ? '填入新密码即可替换。' : '密码在服务端加密保存。'}</small></div></div><div class="field"><label>主机密钥验证</label><select name="host_key_policy"><option value="strict" ${policy === 'strict' ? 'selected' : ''}>严格验证已有主机密钥</option><option value="accept-new" ${policy === 'accept-new' ? 'selected' : ''}>信任首次连接，拒绝后续密钥变化</option></select><small>首次信任会保存主机密钥，之后拒绝变化。</small></div><label class="check"><input type="checkbox" name="enabled" ${v.enabled !== false ? 'checked' : ''}>启用此连接</label><details class="vps-extra" ${v.provider || v.region || v.system || v.notes ? 'open' : ''}><summary>服务商、地区与备注</summary><div class="form-row"><div class="field"><label>服务商</label><input name="provider" maxlength="120" value="${esc(v.provider || '')}" placeholder="可选"></div><div class="field"><label>地区</label><input name="region" maxlength="120" value="${esc(v.region || '')}" placeholder="可选"></div></div><div class="field"><label>系统 / 配置</label><input name="system" maxlength="160" value="${esc(v.system || '')}" placeholder="例如 Debian 13 · 2 核 4 GB"></div><div class="field"><label>备注</label><textarea name="notes" rows="2" maxlength="2000" placeholder="用途或部署目录，不要填写密码">${esc(v.notes || '')}</textarea></div></details></section><section class="form-section vps-form-side"><div class="vps-section-head"><h3>AI 项目授权 · 可选</h3><p class="form-note">打开终端无需分配项目；勾选后允许 AI 在所选项目中连接此服务器。</p></div><div class="field"><label>分配给项目（可多选）</label>${vpsProjectChoices(v.project_ids)}</div></section></div><p id="vps-form-error" class="vps-error" role="alert" hidden></p></form>`,
    `${id ? '<button class="btn danger" type="button" id="vps-delete">删除连接</button>' : ''}<button class="btn ghost" type="button" data-action="close-modal">取消</button><button class="btn" type="button" id="vps-save-terminal">保存并打开终端</button><button class="btn primary" type="button" id="vps-save">保存并检查</button>`,
  );
  dialog.classList.add('vps-edit-dialog');
  const form = $('#vps-form', dialog),
    save = $('#vps-save', dialog),
    saveTerminal = $('#vps-save-terminal', dialog);
  const session = S.session,
    space = S.space_id,
    page = S.page;
  const editCurrent = () =>
    dialog.isConnected && session === S.session && space === S.space_id && page === S.page;
  form.addEventListener('change', (event) => {
    const input = event.target;
    if (input.name === 'project_ids') {
      const hint = input
        .closest('.vps-project-choice')
        ?.querySelector('[data-vps-permission-hint]');
      if (hint) hint.hidden = !input.checked;
    }
  });
  const submit = ({ openTerminal = false } = {}) => {
    if (!form.reportValidity()) return;
    const trigger = openTerminal && saveTerminal ? saveTerminal : save,
      peer = trigger === save ? saveTerminal : save;
    if (peer) peer.disabled = true;
    return busy(trigger, async () => {
      const error = $('#vps-form-error', dialog);
      error.hidden = true;
      try {
        const data = Object.fromEntries(new FormData(form));
        data.port = Number(data.port);
        data.enabled = form.elements.enabled.checked;
        data.project_ids = $$('input[name="project_ids"]:checked', form).map((x) => x.value);
        if (id) data.expected_version = v.version;
        if (!data.password) delete data.password;
        const saved = await api('/api/vps' + (id ? '/' + id : ''), {
          method: id ? 'PUT' : 'POST',
          body: JSON.stringify(data),
        });
        form.elements.password.value = '';
        if (!editCurrent()) return;
        toast('VPS 已保存');
        const targetId = saved.id || id;
        if (openTerminal) {
          delete dialog.dataset.editorDirty;
          form.reset();
          closeModal(dialog);
          if (S.page === 'vps' || S.page === 'projects')
            await renderPage(false).catch((e) => toast(e.message, true));
          if (saved.enabled !== false) {
            if (S.page !== 'vps') await navigate('vps');
            await vpsOpenTerminal(targetId);
          }
          return;
        }
        const check = vpsCheck(targetId, { dialog, saved, autoRun: true });
        if (S.page === 'vps' || S.page === 'projects')
          renderPage(false).catch((e) => toast(e.message, true));
        await check;
      } catch (e) {
        if (dialog.isConnected) {
          error.textContent =
            e.code === 'NETWORK_UNCERTAIN'
              ? '网络中断，保存结果待确认。请刷新 VPS 列表。'
              : e.message;
          error.hidden = false;
        }
      } finally {
        if (peer?.isConnected) peer.disabled = false;
      }
    });
  };
  save.onclick = () => submit();
  if (saveTerminal) saveTerminal.onclick = () => submit({ openTerminal: true });
  form.onsubmit = (e) => {
    e.preventDefault();
    submit();
  };
  if (id)
    $('#vps-delete', dialog).onclick = async () => {
      const button = $('#vps-delete', dialog),
        intent = S.vpsIntent;
      if (button.disabled) return;
      const ok = await uiConfirmAction(
        {
          title: `删除“${v.name}”？`,
          body: '服务器会保留，连接和项目分配将移除。',
          confirmLabel: '删除连接',
          danger: true,
        },
        {
          owner: dialog,
          current: () => $('.modal') === dialog && S.vpsIntent === intent,
        },
      );
      if (!ok) return;
      return busy(button, async () => {
        await api('/api/vps/' + id + '?expected_version=' + v.version, {
          method: 'DELETE',
        });
        form.reset();
        closeModal(dialog);
        toast('已删除连接');
        await renderPage(false);
      });
    };
}
async function vpsAssignProject(projectId) {
  const current = vpsIntent();
  const [items] = await Promise.all([vpsInventory(), loadBasics()]);
  if (!current()) return;
  const project = S.projects.find((p) => p.id === projectId);
  if (!project) throw new Error('项目已变化，请刷新');
  const selected = items.filter((v) => v.project_ids.includes(projectId)).map((v) => v.id);
  const dialog = modal(
    '项目 VPS · ' + project.alias,
    `<p class="form-note">选择这个项目可使用的服务器。连接由 ${esc(project.device_name)} 发起；不会改变其他项目的分配。</p><div class="field"><label>分配 VPS（可多选）</label><div class="check-list vps-check-list">${items.map((v) => `<label class="check"><input type="checkbox" name="vps_ids" value="${esc(v.id)}" ${selected.includes(v.id) ? 'checked' : ''}><span>${esc(v.name)}${v.enabled ? '' : ' · 已停用'}<small>${esc(vpsEndpoint(v))} · ${esc(v.username)}</small></span></label>`).join('') || '<p>还没有 VPS，请先到 VPS 管理添加连接。</p>'}</div></div>`,
    buttons('project-vps-save', '保存分配'),
  );
  $('#project-vps-save', dialog).onclick = () =>
    busy($('#project-vps-save', dialog), async () => {
      await api('/api/projects/' + projectId + '/vps', {
        method: 'PUT',
        body: JSON.stringify({
          vps_ids: $$('input[name="vps_ids"]:checked', dialog).map((x) => x.value),
          expected_vps_ids: selected,
        }),
      });
      closeModal(dialog);
      toast('VPS 分配已更新');
      await renderPage(false);
    });
}
async function vpsCheck(id, { dialog = null, saved = null, autoRun = false } = {}) {
  const session = S.session,
    space = S.space_id,
    current = vpsIntent(),
    v = saved || (await api('/api/vps/' + encodeURIComponent(id)));
  if (!current() || (dialog && !dialog.isConnected)) return;
  const projects = vpsTerminalProjects(v),
    termReason = vpsTerminalReason(v),
    eligible = !!v.enabled && (projects.length > 0 || !termReason),
    canAutoRun = projects.length
      ? !!projects[0].online
      : v.terminal
        ? v.terminal.eligible !== false && v.terminal.online !== false
        : true;
  const title = '检查连接 · ' + v.name;
  const initialState = !v.enabled
    ? '连接已停用。'
    : !eligible
      ? termReason || '当前无法执行连接检查。'
      : autoRun && !canAutoRun
        ? '已保存。可以在连接电脑上线后检查连接。'
        : '等待检查。';
  const body = `<p class="form-note">${esc(vpsEndpoint(v))} · ${esc(v.username)}。读取主机名、系统和磁盘用量。</p><div class="field" hidden><label for="vps-check-project">通过项目连接</label><select id="vps-check-project">${projects.map((p) => `<option value="${esc(p.id)}">${esc(p.alias)} · ${esc(p.device_name)} · ${p.online ? '在线' : '离线'}</option>`).join('')}</select></div>
    <p id="vps-check-state" class="form-note" role="status">${esc(initialState)}</p><div class="code-box"><pre id="vps-check-output"></pre></div>`;
  const footer = `<button class="btn ghost" type="button" data-action="close-modal">关闭</button><button class="btn" type="button" data-vps-action="check-terminal" data-id="${esc(id)}" ${termReason ? 'disabled' : ''}>打开终端</button><button class="btn primary" type="button" id="vps-check-run" ${eligible ? '' : 'disabled'}>开始检查</button>`;
  if (dialog) {
    dialog.classList.remove('modal-editor', 'vps-edit-dialog');
    delete dialog.dataset.presentation;
    delete dialog.dataset.editorDirty;
    delete dialog.dataset.editorForm;
    $('#modal-title', dialog).textContent = title;
    $('.modal-body', dialog).innerHTML = body;
    $('.modal-footer', dialog).innerHTML = footer;
    uiLabelFields(dialog);
    $('#vps-check-run', dialog).focus();
  } else dialog = modal(title, body, footer);
  const button = $('#vps-check-run', dialog),
    status = $('#vps-check-state', dialog),
    output = $('#vps-check-output', dialog),
    select = $('#vps-check-project', dialog);
  let args = null,
    checkKey = null,
    operation = null,
    finished = false;
  button.onclick = async () => {
    if (finished) {
      args = null;
      checkKey = null;
      operation = null;
      finished = false;
    }
    await busy(button, async () => {
      select.disabled = true;
      try {
        if (!operation) {
          if (projects.length > 0) {
            if (!args)
              args = {
                project: select.value || projects[0].id,
                target: 'vps:' + id,
                yield_seconds: 0,
                command: "printf 'CODEPIER_VPS_OK\\n'; hostname; id -un; uname -sr; df -h /",
                timeout_seconds: 30,
                idempotency_key: 'vps-check-' + uid(),
              };
            const result = await tool('exec', args);
            operation = result.operation_id;
          } else {
            if (!checkKey) checkKey = 'vps-check-' + uid();
            status.textContent = '正在检查…';
            const result = await api('/api/vps/' + encodeURIComponent(id) + '/check', {
              method: 'POST',
              body: JSON.stringify({
                timeout_seconds: 30,
                idempotency_key: checkKey,
              }),
            });
            if (!dialog.isConnected || session !== S.session || space !== S.space_id) return;
            if (result.pending && result.operation_id) {
              operation = result.operation_id;
            } else {
              const errObj = result.result?.error;
              const errText =
                typeof result.error === 'string'
                  ? result.error
                  : result.error?.message || errObj?.message || '';
              const succeeded =
                result.state === 'succeeded' || (result.ok === true && !result.state);
              output.textContent =
                result.output ||
                result.result?.data?.output ||
                errText ||
                (succeeded ? '连接检查完成，未返回输出。' : '检查已结束，未返回输出。');
              finished = true;
              status.textContent = succeeded ? '连接检查成功' : errText || '检查未完成';
              return;
            }
          }
        }
        if (!dialog.isConnected) return;
        status.textContent = '正在检查…';
        while (dialog.isConnected && session === S.session && space === S.space_id) {
          const op = await tool('operations_wait', {
            operation_id: operation,
            wait_seconds: 8,
            output_limit: 12000,
          });
          if (!dialog.isConnected || session !== S.session || space !== S.space_id) return;
          const error = op.result?.error;
          output.textContent =
            op.output ||
            op.result?.data?.output ||
            (op.pending
              ? '等待 Agent 返回…'
              : error?.message ||
                op.error ||
                (op.state === 'succeeded'
                  ? '连接检查完成，未返回输出。'
                  : '检查已结束，未返回输出。'));
          status.textContent = op.pending ? '正在检查…' : '检查结束';
          if (!op.pending) {
            finished = true;
            status.textContent =
              op.state === 'succeeded'
                ? '连接检查成功'
                : error?.message || op.error || '检查未完成';
            return;
          }
        }
      } catch (e) {
        if (e.operation_id) operation = e.operation_id;
        if (dialog.isConnected && session === S.session && space === S.space_id) {
          status.textContent = e.message || '检查未完成。';
        }
      }
    });
    if (dialog.isConnected) {
      button.textContent = finished ? '重新检查' : '重试';
      button.disabled = false;
      select.disabled = !finished;
    }
  };
  if (autoRun && eligible && canAutoRun) button.click();
}
document.addEventListener('click', async (e) => {
  const layoutBtn = e.target.closest('[data-vps-layout]');
  if (layoutBtn) {
    const value = layoutBtn.dataset.vpsLayout;
    if (['cards', 'list'].includes(value)) {
      S.vpsLayout = value;
      const grid = $('.vps-grid');
      if (grid) grid.dataset.layout = value;
      $$('[data-vps-layout]').forEach((item) => {
        const active = item.dataset.vpsLayout === value;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      try {
        localStorage.setItem('codepier-vps-layout', value);
      } catch {}
    }
    return;
  }
  const button = e.target.closest('[data-vps-action]');
  if (!button || button.disabled) return;
  try {
    switch (button.dataset.vpsAction) {
      case 'terminal':
        await vpsOpenTerminal(button.dataset.id, button);
        break;
      case 'terminal-new':
        await vpsOpenTerminal(button.dataset.id, button, { newSession: true });
        break;
      case 'resume-session': {
        const sid = button.dataset.session;
        await navigate(
          sid ? 'vps?view=terminal&session=' + encodeURIComponent(sid) : 'vps?view=terminal',
        );
        break;
      }
      case 'sessions':
        await navigate('vps?view=terminal');
        break;
      case 'add':
        await vpsEdit();
        break;
      case 'edit':
        await vpsEdit(button.dataset.id);
        break;
      case 'project':
        await vpsAssignProject(button.dataset.project);
        break;
      case 'check':
        await vpsCheck(button.dataset.id);
        break;
      case 'check-terminal': {
        const activeModal = button.closest('.modal');
        if (activeModal) closeModal(activeModal);
        if (S.page !== 'vps') await navigate('vps');
        await vpsOpenTerminal(button.dataset.id);
        break;
      }
      case 'copy': {
        const v = S.vps?.find((item) => item.id === button.dataset.id);
        if (v) await copy(vpsEndpoint(v));
        break;
      }
      case 'copy-ssh': {
        const v = S.vps?.find((item) => item.id === button.dataset.id);
        if (v) {
          await copy(vpsSshCommand(v), '已复制 SSH 命令');
          if (typeof toast === 'function') toast('已复制 SSH 命令');
        }
        break;
      }
    }
  } catch (error) {
    toast(error.message, true);
  }
});
