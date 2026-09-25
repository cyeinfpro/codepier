// Shared UI primitives, authored as an ES module and bundled without external runtime dependencies.
import { createPanel } from './lifecycle.mjs';

export const $ = (s, r = document) => r.querySelector(s),
  $$ = (s, r = document) => [...r.querySelectorAll(s)];

export const esc = (v) =>
  String(v ?? '').replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );

export const paths = {
  dashboard: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  device: 'M3 4h18v13H3zM8 21h8M12 17v4',
  folder: 'M3 6h6l2 2h10v12H3z',
  code: 'm8 7-5 5 5 5m8-10 5 5-5 5m-3-12-2 20',
  audit: 'M6 3h12v18H6zM9 7h6M9 11h6M9 15h4',
  plug: 'M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3',
  settings:
    'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',
  arrow: 'm9 5 7 7-7 7',
  plus: 'M12 5v14M5 12h14',
  refresh: 'M20 7v5h-5M4 17v-5h5M19 9a7 7 0 0 0-12-4L4 8m1 7a7 7 0 0 0 12 4l3-3',
  logout: 'M9 4H4v16h5M9 12h12m-4-4 4 4-4 4',
  shield: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6',
  close: 'm6 6 12 12M6 18 18 6',
  menu: 'M4 6h16M4 12h16M4 18h16',
  check: 'm5 12 4 4L19 6',
  cloud: 'M6 18a4 4 0 0 1-1-8 7 7 0 0 1 13-2 5 5 0 0 1 0 10z',
  ai: 'M7 5h10v14H7zM10 9h4m-4 4h4M3 9h4m10 0h4M3 15h4m10 0h4M10 2v3m4-3v3m-4 14v3m4-3v3',
  activity: 'M2 12h4l3-8 6 16 3-8h4',
  file: 'M5 3h9l5 5v13H5zM14 3v6h5',
  search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6',
  save: 'M4 3h13l3 3v15H4zM8 3v6h8V3M8 21v-7h8v7',
  copy: 'M8 8h12v13H8zM4 16H2V2h13v3',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  play: 'm8 4 12 8-12 8z',
  stop: 'M6 6h12v12H6z',
  history: 'M4 4v5h5M5 7a9 9 0 1 1-2 8M12 7v5l4 2',
  key: 'M8 4a5 5 0 1 0 0 10 5 5 0 0 0 0-10m4 9 9 8m-5-4 3-3m-6 0 3-3',
  warning: 'm12 3 10 18H2zM12 9v5m0 3v1',
  git: 'M7 3v11a4 4 0 0 0 4 4h4M7 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4m10 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4m0 4v8M17 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4',
  terminal: 'm4 6 5 5-5 5m8 1h8',
  edit: 'm4 15 12-12 5 5L9 20l-6 1zM13 6l5 5',
  trash: 'M3 6h18M5 6l1 15h12l1-15M9 6V3h6v3M9 10v7m6-7v7',
  network: 'M8 3h8v6H8zM2 16h7v5H2zM15 16h7v5h-7zM12 9v4M5 16v-3h14v3',
  lock: 'M5 10h14v11H5zM8 10V6a4 4 0 0 1 8 0v4M12 14v3',
};

export const icon = (name, cls = '') =>
  `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.code}"/></svg>`;

export const stateNames = {
  active: '进行中',
  blocked: '受阻',
  completed: '已验收',
  pending: '待处理',
  skipped: '已跳过',
  reconnecting: '重连恢复中',
  cancelling: '等待取消确认',
  needs_review: '需核实本机结果',
  succeeded: '已完成',
  failed: '失败',
  running: '执行中',
  queued: '排队中',
  unknown: '结果待核实',
  interrupted: '已中断',
  cancelled: '已取消',
  ok: '成功',
  denied: '已拒绝',
  started: '已开始',
};

export const badge = (state) =>
  `<span class="badge ${esc(state)}">${esc(stateNames[state] || state)}</span>`;

export const onlineBadge = (v) =>
  `<span class="badge ${v ? '' : 'offline'}"><i class="dot ${v ? 'online' : 'offline'}"></i>${v ? '在线' : '离线'}</span>`;

export const timeText = (t) =>
  t ? new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚未连接';

export const shortTime = (t) =>
  t ? new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—';

export const json = (v) => JSON.stringify(v, null, 2);

export function uid() {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

export function toast(message, error = false) {
  const host = $('#toasts');
  if (!host) return;
  const text = String(message ?? '');
  const duplicate = $$('.toast', host).find(
    (node) =>
      node.classList.contains('error') === error && $('.toast-message', node)?.textContent === text,
  );
  if (duplicate) return duplicate;
  const origin = document.activeElement,
    node = document.createElement('div');
  node.className = 'toast' + (error ? ' error' : '');
  node.setAttribute('role', error ? 'alert' : 'status');
  node.innerHTML = `${icon(error ? 'warning' : 'check')}<span class="toast-message"></span><button type="button" class="icon-btn toast-dismiss" aria-label="关闭提示">${icon('close')}</button>`;
  $('.toast-message', node).textContent = text;
  host.append(node);
  let timer = null,
    remaining = 4700,
    started = 0,
    hovered = false;
  const remove = () => {
    clearTimeout(timer);
    const focused = node.contains(document.activeElement);
    node.remove();
    if (
      focused &&
      origin?.isConnected &&
      origin.getClientRects().length &&
      !origin.closest('[inert]')
    )
      origin.focus({ preventScroll: true });
  };
  const stop = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (Date.now() - started));
    }
  };
  const resume = () => {
    if (timer || error || hovered || node.contains(document.activeElement) || !node.isConnected)
      return;
    started = Date.now();
    timer = setTimeout(remove, remaining);
  };
  node.addEventListener('mouseenter', () => {
    hovered = true;
    stop();
  });
  node.addEventListener('mouseleave', () => {
    hovered = false;
    resume();
  });
  node.addEventListener('focusin', stop);
  node.addEventListener('focusout', () => queueMicrotask(resume));
  $('.toast-dismiss', node).onclick = remove;
  resume();
  return node;
}

export function download(name, text, type = 'application/json') {
  const u = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a');
  a.href = u;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(u), 3000);
}

export function buttons(primary, label = '保存') {
  return `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" id="${primary}">${esc(label)}</button>`;
}

export function notice(text, info = false) {
  return `<div class="notice ${info ? 'info' : ''}">${icon(info ? 'shield' : 'warning')}<div>${text}</div></div>`;
}

export function empty(text, action = '', label = '') {
  return `<div class="empty">${icon('network')}<p>${esc(text)}</p>${action ? `<button class="btn small" data-action="${action}">${esc(label)}</button>` : ''}</div>`;
}

export function heading(title, eyebrow, subtitle = '', actions = '') {
  return `<header class="page-head"><div>${eyebrow ? `<div class="eyebrow">${esc(eyebrow)}</div>` : ''}<h1>${esc(title)}</h1>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div>${actions ? `<div class="actions">${actions}</div>` : ''}</header>`;
}

export async function busy(button, fn, notify = toast) {
  if (button?.disabled) return;
  const children = button ? [...button.childNodes] : [],
    priorBusy = button?.getAttribute('aria-busy');
  const priorWidth = button?.style.getPropertyValue('min-width'),
    priorPriority = button?.style.getPropertyPriority('min-width');
  if (button) {
    const width = button.getBoundingClientRect().width;
    const label = document.createElement('span');
    label.className = 'ui-busy-label';
    label.append(...children);
    const indicator = document.createElement('span');
    indicator.className = 'ui-busy-indicator';
    indicator.setAttribute('aria-hidden', 'true');
    indicator.innerHTML = '<i class="spinner"></i>';
    button.replaceChildren(label, indicator);
    button.disabled = true;
    button.classList.add('ui-busy');
    button.setAttribute('aria-busy', 'true');
    if (width) button.style.minWidth = width + 'px';
  }
  try {
    return await fn();
  } catch (e) {
    notify(e.message, true);
  } finally {
    if (button) {
      button.replaceChildren(...children);
      button.disabled = false;
      button.classList.remove('ui-busy');
      if (priorBusy === null) button.removeAttribute('aria-busy');
      else button.setAttribute('aria-busy', priorBusy);
      if (priorWidth) button.style.setProperty('min-width', priorWidth, priorPriority);
      else button.style.removeProperty('min-width');
    }
  }
}

export function createModal({
  state: S,
  labelFields: uiLabelFields,
  trapTab: uiTrapTab,
  focusable: uiFocusable,
}) {
  const lifetime = createPanel({ owner: () => S.session });
  function closeModal(expected = null) {
    if (expected && $('.modal') !== expected) return;
    S.vpsIntent = (S.vpsIntent || 0) + 1;
    S.modalIntent = (S.modalIntent || 0) + 1;
    if (S.modalCleanup) S.modalCleanup();
    S.modalCleanup = null;
    $('#modal-root').innerHTML = '';
    $('#app').inert = false;
    document.body.style.overflow = $('.sidebar.open') ? 'hidden' : '';
    let focus = S.modalLastFocus;
    S.modalLastFocus = null;
    if (focus?.isConnected && !focus.getClientRects().length)
      focus = focus.closest('.tool-menu')?.querySelector('summary');
    if (focus?.isConnected && focus.getClientRects().length && !focus.closest('[inert]'))
      focus.focus({ preventScroll: true });
  }
  function modal(title, body, footer = '', large = false, closable = true) {
    const origin = $('.modal') ? S.modalLastFocus : document.activeElement;
    closeModal();
    S.modalLastFocus = origin;
    $('#modal-root').innerHTML =
      `<div class="modal-backdrop"><section class="modal ${large ? 'large' : ''}" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1"><header class="modal-header"><h2 id="modal-title">${esc(title)}</h2>${closable ? `<button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button>` : ''}</header><div class="modal-body">${body}</div>${footer ? `<footer class="modal-footer">${footer}</footer>` : ''}</section></div>`;
    document.body.style.overflow = 'hidden';
    $('#app').inert = true;
    const dialog = $('.modal');
    uiLabelFields(dialog);
    const key = (e) => {
      if (e.key === 'Escape' && closable) {
        e.preventDefault();
        closeModal(dialog);
        return;
      }
      uiTrapTab(e, dialog);
    };
    const scope = lifetime.mount(dialog);
    scope.listen(document, 'keydown', key);
    S.modalCleanup = () => scope.dispose();
    // Establish focus while opening; a delayed callback could steal focus after
    // the user has already moved to another field or started submitting the form.
    const editable = $(
      'input:not(:disabled),textarea:not(:disabled),select:not(:disabled)',
      dialog,
    );
    (editable?.getClientRects().length ? editable : uiFocusable(dialog)[0] || dialog).focus({
      preventScroll: true,
    });
    return dialog;
  }
  return { modal, closeModal };
}
