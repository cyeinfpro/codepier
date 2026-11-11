// Presentation: room UI behavior; lifecycle state is owned by index.mjs.
import {
  base,
  batchPath,
  currentOwner,
  members,
  panel,
  projectLabel,
  rawPartitionTitle,
  readOnlyReason,
  roomProjects,
  root,
  safeChannelActions,
  safeWorkActions,
  state,
  store,
  title,
} from './index.mjs';

export function mutationAllowed(method, url, body, roomId = state.roomId) {
  if (url === base + '/rooms' || store.writable(roomId)) return true;
  if (
    method === 'POST' &&
    (url === batchPath || /^\/api\/collaboration\/connections\/[^/]+\/revoke$/.test(url))
  )
    return true;
  if (url === store.path(roomId) + '/migration') return body.action === 'activate';
  if (url.startsWith(store.path(roomId) + '/legacy-channels/') && url.endsWith('/pause'))
    return ['weixin', 'telegram'].includes(body.channel);
  if (url === store.path(roomId) && method === 'PATCH')
    return Object.keys(body).every((name) =>
      ['title', 'state', 'expected_version', 'idempotency_key'].includes(name),
    );
  if (url.startsWith(base + '/connect-flows/'))
    return ['migrate', 'rebind', 'refresh_invite', 'resume', 'pause', 'remove'].includes(
      body.action,
    );
  if (url.startsWith(base + '/channel-connect-flows/')) return safeChannelActions.has(body.action);
  if (url.startsWith(base + '/work/')) return safeWorkActions.has(body.action);
  return false;
}

export function requireMutation(method, url, body, roomId) {
  if (mutationAllowed(method, url, body, roomId)) return;
  const message = readOnlyReason();
  panel ? panelError(message) : announce(message, true);
  throw Object.assign(new Error(message), { code: 'ROOM_READ_ONLY' });
}

export function roomName(room) {
  const raw = String(room?.title || '').trim();
  const match = rawPartitionTitle.exec(raw);
  if (!match) return raw || '未命名房间';
  const alias = projectLabel(match[1], room);
  const env = match[2] && match[2] !== 'production' ? ' · ' + match[2] : '';
  return (alias || '项目 ' + match[1].slice(0, 6)) + env;
}

export function roomSubtitle(room) {
  if (!room) return '';
  if (room.state === 'archived') return '已归档';
  if (room.state === 'paused') return '已暂停';
  const names = roomProjects(room);
  const title = roomName(room);
  const count = (room.projects || []).filter((p) => p.state !== 'removed').length;
  const projects =
    names.length === 1 && names[0] === title
      ? '1 个项目'
      : names.length
        ? names.slice(0, 2).join('、') + (names.length > 2 ? ` 等 ${names.length} 个项目` : '')
        : count
          ? `${count} 个项目`
          : '无关联项目';
  return projects;
}

export function roomChip(room) {
  if (!room) return null;
  if (room.legacy) return { tone: 'warning', text: '只读 · 待迁移' };
  if (room.state === 'archived') return { tone: 'muted', text: '已归档' };
  if (room.state === 'paused') return { tone: 'muted', text: '已暂停' };
  const list = members();
  if (!list.length) return { tone: 'muted', text: '还没有 dot' };
  const ready = list.filter((m) => ['ready_chat', 'ready_work'].includes(m.stage || m.state));
  return ready.length
    ? { tone: 'success', text: `${ready.length}/${list.length} 个 dot 在线` }
    : { tone: 'warning', text: '等待 dot 接入' };
}

export function selection(value) {
  try {
    const key = 'codepier-room:' + currentOwner();
    if (value !== undefined) sessionStorage.setItem(key, value);
    return sessionStorage.getItem(key) || '';
  } catch {
    return '';
  }
}

export function announce(value, error = false) {
  const node = root?.querySelector('[data-cr-feedback]');
  if (node) {
    node.textContent = value || '';
    node.classList.toggle('is-error', error);
  }
}

export function panelError(value) {
  const node = root?.querySelector('[data-cr-panel-error]');
  if (node) {
    node.textContent = value || '';
    node.hidden = !value;
    // The alert follows a potentially long project list. Keep a rejected
    // save visible without moving keyboard focus or scrolling a closed panel.
    if (value && node.closest('dialog')?.open) node.scrollIntoView({ block: 'nearest' });
  }
}

export function remember() {
  const editor = root?.querySelector('#cr-message');
  if (editor && state.roomId) store.draft().text = editor.value;
}

export function rememberReading() {
  const feed = root?.querySelector('.cr-feed');
  if (!feed || !state.roomId || !state.room || root._restoringReading) return;
  store.draft().reading = readingSnapshot();
}

export function readingSnapshot() {
  const feed = root?.querySelector('.cr-feed');
  if (!feed) return null;
  const edge = feed.getBoundingClientRect().top;
  const anchor = [...root.querySelectorAll('[data-message-id], [data-work-id]')].find(
    (node) => node.getBoundingClientRect().bottom > edge + 8,
  );
  return {
    anchorId: anchor?.dataset.messageId || '',
    workId: anchor?.dataset.workId || '',
    offset: anchor ? anchor.getBoundingClientRect().top - edge : 0,
    atEnd: feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90,
    visibilityRevision: state.visibilityRevision,
  };
}

export function restoreAnchor(reading) {
  if (!reading || reading.atEnd || !root) return;
  const feed = root.querySelector('.cr-feed');
  const anchor = [...root.querySelectorAll('[data-message-id], [data-work-id]')].find((node) =>
    reading.anchorId
      ? node.dataset.messageId === reading.anchorId
      : node.dataset.workId === reading.workId,
  );
  if (anchor)
    feed.scrollTop +=
      anchor.getBoundingClientRect().top - feed.getBoundingClientRect().top - reading.offset;
}

export async function restoreReading() {
  const view = root,
    id = state.roomId,
    epoch = state.epoch;
  const reading = store.draft().reading;
  if (!view) return;
  const wasRestoring = view._restoringReading;
  view._restoringReading = !!reading;
  if (!reading) {
    if (wasRestoring)
      view.querySelector('.cr-feed').scrollTop = view.querySelector('.cr-feed').scrollHeight;
    return;
  }
  const current = () => root === view && id === state.roomId && epoch === state.epoch;
  const findAnchor = () =>
    [...view.querySelectorAll('[data-message-id], [data-work-id]')].find((node) =>
      reading.anchorId
        ? node.dataset.messageId === reading.anchorId
        : node.dataset.workId === reading.workId,
    );
  try {
    if (
      current() &&
      store.draft().reading === reading &&
      !reading.atEnd &&
      !findAnchor() &&
      reading.anchorId
    )
      await store.restoreMessage(reading.anchorId);
    if (
      current() &&
      store.draft().reading === reading &&
      !reading.atEnd &&
      reading.workId &&
      !findAnchor()
    )
      await store.restoreWork(reading.workId);
    if (!current() || store.draft().reading !== reading) return;
    const feed = view.querySelector('.cr-feed'),
      anchor = findAnchor();
    if (reading.atEnd) feed.scrollTop = feed.scrollHeight;
    else if (anchor) restoreAnchor(reading);
    else {
      delete store.draft().reading;
      announce('原阅读位置已不可见，已保留当前可读取的历史。');
    }
  } catch (error) {
    if (!current()) return;
    delete store.draft().reading;
    await store.latestMessages();
    announce(
      [400, 403, 404, 410].includes(error.status)
        ? '原阅读位置已不在当前可读范围，已核对最新窗口。'
        : '原阅读位置暂时无法恢复，已保留草稿并核对最新窗口。',
    );
  } finally {
    if (current()) view._restoringReading = false;
  }
}
