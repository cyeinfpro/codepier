'use strict';
// Explicit desktop inspection only. Screenshots and session IDs are never persisted in browser storage.
async function computerPanel(project = S.work.project) {
  if (!project) {
    toast('请先选择项目', true);
    return;
  }
  const login = S.session;
  const dialog = modal(
    '桌面控制 · 本机联调',
    `<section class="computer-panel"><div class="computer-toolbar"><strong id="computer-state" role="status">检查本机…</strong><span id="computer-ttl" class="muted tiny"></span><button class="btn small" id="computer-probe">检查接口</button><button class="btn small" id="computer-apps">应用列表</button><button class="btn small danger" id="computer-stop">停止桌面会话</button></div><form id="computer-open-form" class="computer-open"><input name="app" aria-label="应用名称或 Bundle ID" placeholder="已授权的应用名称或 Bundle ID" maxlength="512" required><button class="btn primary">连接并读取</button></form><div class="actions"><button class="btn" id="computer-observe" disabled>重新读屏</button><button class="btn ghost" id="computer-close" disabled>结束联调</button></div><p id="computer-message" class="form-note" role="status"></p><div id="computer-frame" class="computer-frame"><p class="muted">此处只在你点击后读取画面，不自动录屏。</p></div><details class="computer-accessibility"><summary>应用内容与接口信息</summary><pre id="computer-tree"></pre></details><p class="form-note">联调会话属于本面板；交给 ChatGPT 操作前先结束联调。ChatGPT 使用 computer_action 进行点击、拖拽、滚动、按键和文本操作。</p></section>`,
    '',
    true,
  );
  let lease = null,
    expires = 0,
    seq = 0,
    disposed = false,
    working = false;
  const q = (s) => $(s, dialog),
    state = q('#computer-state'),
    message = q('#computer-message');
  const current = (n) =>
    !disposed && dialog.isConnected && S.session === login && (n === undefined || n === seq);
  function sync() {
    q('#computer-observe').disabled = working || !lease;
    q('#computer-close').disabled = !lease;
    q('#computer-open-form button').disabled = working || !!lease;
    q('#computer-open-form input').disabled = working || !!lease;
  }
  async function release(id) {
    if (id && S.session === login)
      await settled(
        tool('computer_session_close', {
          project,
          session_id: id,
          idempotency_key: 'computer-close-' + uid(),
        }),
      );
  }
  async function run(fn) {
    if (working) return;
    const n = ++seq;
    working = true;
    sync();
    try {
      await fn(n);
    } catch (e) {
      if (current(n)) message.textContent = e.message;
    } finally {
      if (current(n)) {
        working = false;
        sync();
      }
    }
  }
  function show(r) {
    q('#computer-tree').textContent = r.text || r.note || '';
    const frame = q('#computer-frame');
    frame.replaceChildren();
    if (!r.media_expired) {
      for (const b of r._computer_content || []) {
        if (
          b.type !== 'image' ||
          !['image/png', 'image/jpeg'].includes(b.mimeType) ||
          typeof b.data !== 'string'
        )
          continue;
        const img = document.createElement('img');
        img.alt = '当前应用截图';
        img.src = `data:${b.mimeType};base64,${b.data}`;
        frame.append(img);
      }
    }
    if (!frame.childElementCount) {
      const p = document.createElement('p');
      p.className = 'muted';
      p.textContent = r.media_expired
        ? '截图已过期，请重新读取。'
        : '本次没有截图，请查看应用内容或本机授权提示。';
      frame.append(p);
    }
    message.textContent = r.native_is_error
      ? '原生工具拒绝读取；请在本机处理权限提示。'
      : r.note || '本次画面已读取，不会自动连续录屏。';
  }
  async function observe(n) {
    const r = await settled(tool('computer_observe', { project, session_id: lease }));
    if (current(n)) show(r);
  }
  q('#computer-open-form').onsubmit = (e) => {
    e.preventDefault();
    run(async (n) => {
      const r = await settled(
        tool('computer_session_open', {
          project,
          app: q('#computer-open-form input').value.trim(),
          ttl_seconds: 300,
          idempotency_key: 'computer-open-' + uid(),
        }),
      );
      if (!current(n)) {
        await release(r.session_id);
        return;
      }
      lease = r.session_id;
      expires = r.expires_at;
      state.textContent = '已连接 · ' + r.app;
      sync();
      await observe(n);
    });
  };
  q('#computer-observe').onclick = () => run(observe);
  q('#computer-close').onclick = () => {
    const id = lease;
    const n = ++seq;
    lease = null;
    expires = 0;
    working = false;
    sync();
    state.textContent = '正在结束…';
    release(id)
      .then(() => {
        if (current(n)) state.textContent = '联调已结束';
      })
      .catch((e) => {
        if (current(n)) message.textContent = e.message;
      });
  };
  q('#computer-stop').onclick = () => {
    const n = ++seq;
    lease = null;
    expires = 0;
    working = false;
    sync();
    state.textContent = '正在停止…';
    settled(
      tool('computer_session_close', {
        project,
        force: true,
        idempotency_key: 'computer-stop-' + uid(),
      }),
    )
      .then((r) => {
        if (current(n)) {
          state.textContent = '会话已停止';
          message.textContent = r.note;
        }
      })
      .catch((e) => {
        if (current(n)) message.textContent = e.message;
      });
  };
  q('#computer-probe').onclick = () =>
    run(async (n) => {
      const r = await settled(tool('computer_status', { project, probe: true }));
      if (current(n)) {
        state.textContent = r.capabilities_verified ? '接口已验证' : '接口待检查';
        message.textContent =
          '原生工具：' + (r.native_tools || []).join(' / ') + '。系统和应用权限需读屏时验证。';
      }
    });
  q('#computer-apps').onclick = () =>
    run(async (n) => {
      const r = await settled(tool('computer_apps', { project }));
      if (current(n)) {
        q('#computer-tree').textContent = r.text || '';
        q('.computer-accessibility').open = true;
        message.textContent = r.note || '';
      }
    });
  const ticker = setInterval(() => {
    if (!current()) return;
    const left = Math.max(0, Math.ceil(expires - Date.now() / 1000));
    q('#computer-ttl').textContent = lease ? `剩余 ${left} 秒` : '';
    if (lease && !left) {
      lease = null;
      state.textContent = '联调已过期';
      sync();
    }
  }, 1000);
  const cleanup = S.modalCleanup;
  S.modalCleanup = () => {
    disposed = true;
    ++seq;
    clearInterval(ticker);
    const id = lease;
    lease = null;
    cleanup?.();
    release(id).catch(() => {});
  };
  const initial = seq;
  try {
    const r = await settled(tool('computer_status', { project }));
    if (current(initial)) {
      state.textContent = r.locally_stopped
        ? '本机已停止'
        : r.enabled
          ? '本机已授权'
          : '本机未开启';
      message.textContent = r.enabled
        ? '选择已在本机授权的应用。'
        : '在本机 configure 中显式开启 computer，并选择项目和应用。旧凭据不会自动获得桌面权限。';
    }
  } catch (e) {
    if (current(initial)) message.textContent = e.message;
  }
}
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-computer-use]') && S.session)
    computerPanel().catch((error) => toast(error.message, true));
});

// Human approval inbox is separate from the busy observe dialog and from MCP.
// No persisted decisions, no automatic acceptance, no credential entry forms.
const computerApprovals = {
  login: null,
  entries: new Map(),
  resolved: new Map(),
  request: null,
  refreshAgain: false,
  timer: null,
  expiryTimer: null,
  connected: false,
  backoff: 5000,
};
function stopComputerApprovals() {
  clearTimeout(computerApprovals.timer);
  clearTimeout(computerApprovals.expiryTimer);
  computerApprovals.timer = computerApprovals.expiryTimer = null;
  computerApprovals.login = null;
  computerApprovals.refreshAgain = false;
  computerApprovals.connected = false;
  computerApprovals.entries.clear();
  computerApprovals.resolved.clear();
  document.getElementById('computer-approval-inbox')?.remove();
}
function startComputerApprovals() {
  if (computerApprovals.login !== S.session) {
    stopComputerApprovals();
    computerApprovals.login = S.session;
    computerApprovals.backoff = 5000;
  }
  return refreshComputerApprovals();
}
function scheduleComputerApprovals(delay) {
  clearTimeout(computerApprovals.timer);
  computerApprovals.timer = null;
  if (S.session && computerApprovals.login === S.session && !document.hidden)
    computerApprovals.timer = setTimeout(refreshComputerApprovals, delay);
}
function computerApprovalsConnection(connected) {
  const wasConnected = computerApprovals.connected;
  computerApprovals.connected = connected;
  if (connected) refreshComputerApprovals();
  else if (wasConnected || !computerApprovals.timer) scheduleComputerApprovals(5000);
}
function removeComputerApproval(identifier) {
  const entry = computerApprovals.entries.get(identifier);
  entry?.card.remove();
  computerApprovals.entries.delete(identifier);
  if (!computerApprovals.entries.size) document.getElementById('computer-approval-inbox')?.remove();
}
function updateComputerApproval(entry) {
  const expired = entry.item.expires_at * 1000 <= Date.now();
  const state = entry.pending
    ? 'pending'
    : expired
      ? 'expired'
      : entry.verified
        ? 'ready'
        : 'unavailable';
  entry.card.dataset.state = state;
  entry.card.setAttribute('aria-busy', String(entry.pending));
  entry.buttons.forEach((button) => (button.disabled = state !== 'ready'));
  entry.status.textContent = entry.pending
    ? '正在提交决定…'
    : expired
      ? '授权请求已过期，请重新读屏。'
      : entry.verified
        ? '等待你选择 · ' +
          new Date(entry.item.expires_at * 1000).toLocaleTimeString('zh-CN', { hour12: false }) +
          ' 到期'
        : '授权状态暂时无法确认，正在重新连接…';
}
function expireComputerApprovals() {
  clearTimeout(computerApprovals.expiryTimer);
  computerApprovals.expiryTimer = null;
  if (!S.session || computerApprovals.login !== S.session || document.hidden) return;
  const now = Date.now();
  let next = Infinity,
    expired = false;
  for (const [identifier, entry] of computerApprovals.entries) {
    if (entry.pending) continue;
    const deadline = entry.item.expires_at * 1000;
    if (deadline <= now) {
      entry.removeAt ??= now + 3000;
      updateComputerApproval(entry);
      expired = true;
      if (entry.removeAt <= now) {
        removeComputerApproval(identifier);
        continue;
      }
      next = Math.min(next, entry.removeAt);
    } else next = Math.min(next, deadline);
  }
  if (Number.isFinite(next))
    computerApprovals.expiryTimer = setTimeout(expireComputerApprovals, Math.max(1, next - now));
  if (expired) scheduleComputerApprovals(3000);
}
function createComputerApproval(item, login, box) {
  const card = document.createElement('section');
  card.dataset.requestId = item.request_id;
  const title = document.createElement('h3');
  title.textContent = '本机应用请求授权';
  card.append(title);
  const details = document.createElement('p'),
    text = document.createElement('p');
  card.append(details, text);
  const note = document.createElement('p');
  note.textContent =
    '允许后，当前桌面会话可访问该应用；原生提供方可能记住应用权限。此决定不扩展项目或 MCP 授权。请由你本人选择。';
  card.append(note);
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  card.append(status);
  const entry = { item, card, details, text, status, buttons: [], pending: false, verified: true };
  for (const [action, label] of [
    ['accept', '允许应用访问'],
    ['decline', '拒绝'],
    ['cancel', '取消请求'],
  ]) {
    const button = document.createElement('button');
    button.className = 'btn small';
    button.textContent = label;
    button.onclick = async () => {
      if (
        S.session !== login ||
        computerApprovals.login !== login ||
        computerApprovals.entries.get(item.request_id) !== entry ||
        entry.pending ||
        !entry.verified ||
        entry.item.expires_at * 1000 <= Date.now()
      )
        return;
      entry.pending = true;
      updateComputerApproval(entry);
      try {
        await post('/api/computer/approvals/' + encodeURIComponent(item.request_id), { action });
        if (S.session === login && computerApprovals.entries.get(item.request_id) === entry) {
          computerApprovals.resolved.set(item.request_id, entry.item.expires_at * 1000);
          removeComputerApproval(item.request_id);
        }
      } catch (error) {
        if (S.session !== login || computerApprovals.entries.get(item.request_id) !== entry) return;
        entry.pending = false;
        entry.verified = false;
        if (error.code === 'APPROVAL_EXPIRED' || error.status === 409) {
          entry.item = { ...entry.item, expires_at: 0 };
        }
        updateComputerApproval(entry);
        toast(error.message, true);
      } finally {
        if (S.session === login && computerApprovals.login === login) {
          expireComputerApprovals();
          refreshComputerApprovals();
        }
      }
    };
    entry.buttons.push(button);
    card.append(button);
  }
  box.append(card);
  computerApprovals.entries.set(item.request_id, entry);
  return entry;
}
function renderComputerApprovals(items, login) {
  for (const [identifier, deadline] of computerApprovals.resolved)
    if (deadline <= Date.now()) computerApprovals.resolved.delete(identifier);
  // A GET started before a submitted decision must not resurrect its old card.
  items = items.filter((item) => !computerApprovals.resolved.has(item.request_id));
  let box = document.getElementById('computer-approval-inbox');
  const live = new Set(items.map((item) => item.request_id));
  for (const [identifier, entry] of computerApprovals.entries) {
    if (!live.has(identifier) && !entry.pending) removeComputerApproval(identifier);
  }
  if (items.length && !box?.isConnected) {
    box = document.createElement('aside');
    box.id = 'computer-approval-inbox';
    box.setAttribute('aria-label', '本机应用授权请求');
    document.body.append(box);
  }
  for (const item of items) {
    const entry =
      computerApprovals.entries.get(item.request_id) || createComputerApproval(item, login, box);
    entry.item = item;
    entry.verified = true;
    const project = S.projects.find((project) => project.id === item.project_id);
    entry.details.textContent = `项目：${project?.alias || item.project_id} · 应用：${item.app} · 发起者：${item.owner} · 会话：${item.session_id}`;
    entry.text.textContent = item.message;
    updateComputerApproval(entry);
  }
  expireComputerApprovals();
}
async function refreshComputerApprovals() {
  if (computerApprovals.login !== S.session) {
    stopComputerApprovals();
    computerApprovals.login = S.session;
  }
  if (!S.session || document.hidden) return;
  if (computerApprovals.request) {
    computerApprovals.refreshAgain = true;
    return;
  }
  clearTimeout(computerApprovals.timer);
  computerApprovals.timer = null;
  const login = S.session,
    request = {};
  computerApprovals.request = request;
  computerApprovals.refreshAgain = false;
  let delay = computerApprovals.connected ? 15000 : 5000;
  try {
    // The inbox owns its retry schedule; a stalled GET must not hide newer SSE events for a minute.
    const result = await api('/api/computer/approvals', { retryDelays: [], requestTimeout: 5000 });
    if (S.session !== login || computerApprovals.login !== login) return;
    renderComputerApprovals(result.approvals, login);
    computerApprovals.backoff = 5000;
  } catch (error) {
    if (S.session !== login || computerApprovals.login !== login) return;
    for (const entry of computerApprovals.entries.values()) {
      entry.verified = false;
      updateComputerApproval(entry);
    }
    delay = computerApprovals.backoff;
    computerApprovals.backoff = Math.min(30000, delay * 2);
  } finally {
    if (computerApprovals.request === request) computerApprovals.request = null;
    // Events received during the GET may describe a newer inbox than its response.
    if (computerApprovals.login === S.session && S.session)
      scheduleComputerApprovals(computerApprovals.refreshAgain ? 0 : delay);
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(computerApprovals.timer);
    clearTimeout(computerApprovals.expiryTimer);
  } else if (S.session) {
    expireComputerApprovals();
    refreshComputerApprovals();
  }
});
window.addEventListener('online', () => {
  if (S.session) refreshComputerApprovals();
});
