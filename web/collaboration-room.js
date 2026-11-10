'use strict';
// Room-first panel. Authorization, readiness and recovery actions come from Hub.
window.CodePierCollaboration = (() => {
  const store = window.CodePierRoomStore.create((...args) => api(...args));
  const state = store.state;
  const E = (value) => CP.ui.esc(String(value ?? ''));
  const icon = (name) =>
    name === 'message'
      ? '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z"/></svg>'
      : CP.ui.icon(name);
  const btn = (action, label, extra = '', cls = '') =>
    `<button type="button" class="cr-button ${cls}" data-cr-action="${action}" ${extra}>${label}</button>`;
  const field = (label, control) =>
    `<label class="cr-field"><span>${label}</span>${control}</label>`;
  const base = '/api/collaboration';
  const stages = {
    invited: '等待 dot 接入',
    joining: '正在连接',
    waiting_host: '等待在 dot 中确认',
    verifying: '正在验证回复',
    ready_chat: '可以聊天',
    ready_work: '可以协作',
    degraded: '需要恢复连接',
    paused: '已暂停',
    removed: '已移除',
    choose_account: '选择微信账号',
    scan: '扫码登录',
    confirm_account: '确认微信账号',
    pairing: '确认本人',
    confirm_owner: '确认房间与范围',
    ready: '微信已连接',
    expired: '连接已过期',
  };
  const workLabels = {
    queued: '排队中',
    leased: '排队中',
    running: '进行中',
    verifying: '进行中',
    waiting_input: '等你回复',
    waiting_user: '等你回复',
    waiting_member: '等待中',
    waiting_authorization: '等待中',
    waiting_target: '等待中',
    reconciling: '等待中',
    blocked: '等待中',
    unknown: '等待中',
    reconnecting: '等待中',
    succeeded: '已完成',
    completed: '已完成',
    paused: '已暂停',
    cancelling: '正在停止',
    stopping: '正在停止',
    cancelled: '已停止',
    failed: '失败',
    expired: '已过期',
  };
  const memberBadges = {
    invited: '连接中',
    joining: '连接中',
    waiting_host: '连接中',
    verifying: '连接中',
    ready_chat: '在线',
    ready_work: '在线',
    degraded: '需要处理',
    paused: '已暂停',
    removed: '已移除',
  };
  const deliveryLabels = {
    queued: '待发送',
    pending: '待发送',
    preparing: '待发送',
    leased: '待发送',
    sending: '发送中',
    waiting_context: '等待发送条件',
    waiting_media: '等待附件',
    blocked: '等待前一条回复',
    accepted: '已受理',
    owner_confirmed: '房主已确认收到',
    needs_attention: '发送失败',
    attention: '发送失败',
    rejected: '发送失败',
    failed: '发送失败',
    unknown: '送达待核对',
    cancelled: '已取消',
    superseded: '已替换',
  };
  const finishedWork = (work) =>
    ['succeeded', 'completed', 'cancelled', 'failed', 'expired'].includes(work.state);
  const contextKinds = {
    objective: '当前目标',
    decision: '已确认决策',
    constraint: '当前约束',
    question: '待回答问题',
    artifact: '产物引用',
  };
  let root = null,
    timer = null,
    bound = null,
    panel = null,
    panelEpoch = 0,
    pollPromise = null,
    pollAgain = false,
    hints = null,
    hintsConnected = false,
    accessBlocked = false;
  let requestedProject = '',
    unsubscribe = null,
    abortEvents = null,
    suspended = false;
  const activeRoom = () => store.writable();
  const readOnlyReason = () =>
    state.room?.legacy
      ? '这是旧版房间。处理完迁移清单后即可继续使用。'
      : '房间已暂停或归档，恢复后才能发消息。';
  const retryableContinuationPreview = new Set([
    'ROOM_CONTINUATION_PREVIEW_EXPIRED',
    'ROOM_CONTINUATION_PREVIEW_CHANGED',
    'ROOM_CONTINUATION_SELECTION_CHANGED',
  ]);
  const safeWorkActions = new Set(['pause', 'cancel', 'stop']);
  const safeChannelActions = new Set(['pause', 'disconnect', 'revoke_tasks', 'cancel']);
  function mutationAllowed(method, url, body, roomId = state.roomId) {
    if (url === base + '/rooms' || store.writable(roomId)) return true;
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
    if (url.startsWith(base + '/channel-connect-flows/'))
      return safeChannelActions.has(body.action);
    if (url.startsWith(base + '/work/')) return safeWorkActions.has(body.action);
    return false;
  }
  function requireMutation(method, url, body, roomId) {
    if (mutationAllowed(method, url, body, roomId)) return;
    const message = readOnlyReason();
    panel ? panelError(message) : announce(message, true);
    throw Object.assign(new Error(message), { code: 'ROOM_READ_ONLY' });
  }
  const currentOwner = () =>
    [S.session?.user_id || S.session?.username || '', S.space_id || ''].join(':');
  const memberId = (item) => item.member_id || item.id;
  const memberShort = (item) => '#' + memberId(item).slice(0, 8);
  const memberOrigin = (item) =>
    item.host_label ||
    (item.connection_source_member_id
      ? '来自连接 #' + item.connection_source_member_id.slice(0, 8)
      : item.legacy
        ? '原连接待迁移'
        : item.evidence?.host_bound
          ? '本房间已登记的宿主'
          : '宿主尚未登记');
  const members = () => (state.room?.members || []).filter((item) => item.state !== 'removed');
  const title = (item) => item?.display?.title || stages[item?.stage || item?.state] || '等待连接';
  // Rooms carried over from project partitions were titled with raw project
  // IDs ("267454898aed… · production"). Show the project alias instead.
  const rawPartitionTitle = /^([0-9a-f]{32})(?:\s*·\s*(.+))?$/i;
  const projectLabel = (id, room) =>
    S.projects?.find((p) => p.id === id)?.alias ||
    S.projects?.find((p) => p.id === id)?.name ||
    room?.projects?.find((p) => p.project_id === id)?.project_label ||
    room?.projects?.find((p) => p.project_id === id)?.alias ||
    '';
  function roomName(room) {
    const raw = String(room?.title || '').trim();
    const match = rawPartitionTitle.exec(raw);
    if (!match) return raw || '未命名房间';
    const alias = projectLabel(match[1], room);
    const env = match[2] && match[2] !== 'production' ? ' · ' + match[2] : '';
    return (alias || '项目 ' + match[1].slice(0, 6)) + env;
  }
  const roomProjects = (room) =>
    (room?.projects || [])
      .filter((p) => p.state !== 'removed')
      .map((p) => projectLabel(p.project_id, room) || p.project_id?.slice(0, 6))
      .filter(Boolean);
  function roomSubtitle(room) {
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
  function roomChip(room) {
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
  let roomFilter = '';
  const hue = (value) =>
    [...String(value || '')].reduce((sum, char) => (sum * 31 + char.charCodeAt(0)) % 360, 7);
  const actionLabel = (action) =>
    ({
      refresh_invite: '刷新加入说明',
      resume: '继续连接',
      retry_probe: '重新发送连接检查',
      pause: '暂停',
      remove: '移除成员',
      continue_connection: '继续连接',
      recover_connection: '恢复连接',
      copy_instructions: '复制加入说明',
      view_details: '查看详情',
      start_qr: '生成二维码',
      poll_qr: '检查扫码状态',
      accept_account: '确认账号',
      bind: '生成配对说明',
      confirm_owner: '完成连接',
      refresh_pairing: '刷新配对说明',
      cancel: '取消连接',
      approve_tasks: '允许微信交办工作',
      revoke_tasks: '仅撤销工作许可',
      rebind: '更换连接',
    })[action] || '继续';
  const time = (value) =>
    value
      ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleTimeString('zh-CN', {
          hour: '2-digit',
          minute: '2-digit',
        })
      : '';
  const dateTime = (value) =>
    value === null || value === undefined || value === ''
      ? '未提供'
      : new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN');
  const supplied = (value) => (value === null || value === undefined ? '未提供' : String(value));
  const identifier = (value) => (value ? '#' + String(value).slice(0, 12) : '未提供');
  function selection(value) {
    try {
      const key = 'codepier-room:' + currentOwner();
      if (value !== undefined) sessionStorage.setItem(key, value);
      return sessionStorage.getItem(key) || '';
    } catch {
      return '';
    }
  }
  function announce(value, error = false) {
    const node = root?.querySelector('[data-cr-feedback]');
    if (node) {
      node.textContent = value || '';
      node.classList.toggle('is-error', error);
    }
  }
  function panelError(value) {
    const node = root?.querySelector('[data-cr-panel-error]');
    if (node) {
      node.textContent = value || '';
      node.hidden = !value;
      // The alert follows a potentially long project list. Keep a rejected
      // save visible without moving keyboard focus or scrolling a closed panel.
      if (value && node.closest('dialog')?.open) node.scrollIntoView({ block: 'nearest' });
    }
  }
  function remember() {
    const editor = root?.querySelector('#cr-message');
    if (editor && state.roomId) store.draft().text = editor.value;
  }
  function rememberReading() {
    const feed = root?.querySelector('.cr-feed');
    if (!feed || !state.roomId || !state.room || root._restoringReading) return;
    store.draft().reading = readingSnapshot();
  }
  function readingSnapshot() {
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
  function restoreAnchor(reading) {
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
  async function restoreReading() {
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
  function detach() {
    remember();
    rememberReading();
    clearTimeout(timer);
    timer = null;
    closeHints();
    pollAgain = false;
    store.abort();
    abortEvents?.abort();
    abortEvents = null;
    unsubscribe?.();
    unsubscribe = null;
    CP.ui.closeModalLayer(root?.querySelector('dialog'));
    root = null;
    bound = null;
    panelEpoch++;
    panel = null;
  }
  function clear() {
    detach();
    store.reset();
  }
  async function html() {
    if (state.owner !== currentOwner()) store.reset(currentOwner());
    try {
      await store.list();
    } catch (error) {
      if (error.code === 'COLLABORATION_DISABLED')
        return '<section class="panel cr-disabled"><h1 tabindex="-1">协作中心</h1><p>协作中心尚未启用。</p></section>';
      throw error;
    }
    const selected =
      state.rooms.find((item) => item.id === (state.roomId || selection())) ||
      (requestedProject &&
        state.rooms.find((item) =>
          (item.projects || []).some((p) => p.project_id === requestedProject),
        )) ||
      state.rooms[0];
    if (selected) {
      try {
        await store.select(selected.id);
      } catch (error) {
        if (error.name !== 'AbortError') state.loadError = error.message;
      }
    } else await store.select('');
    return `<section class="collaboration cr-shell" aria-label="协作中心">
      <aside class="cr-sidebar" aria-label="房间列表"><header class="cr-side-head"><div><h2>房间</h2><span class="cr-count" data-cr-room-count></span></div>${btn('create', icon('plus') + '<span>新建</span>', 'aria-label="新建房间"', 'cr-primary cr-new-room')}</header><label class="cr-search"><span class="sr-only">搜索房间</span><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg><input type="search" data-cr-room-filter placeholder="搜索房间或项目" autocomplete="off"></label><div class="cr-room-list" data-cr-rooms></div>${btn('more-rooms', '加载更多房间', 'data-cr-more-rooms hidden', 'cr-text-button cr-full')}<footer>${btn('archives', '显示已归档', 'aria-pressed="false"', 'cr-text-button')}</footer></aside>
      <main class="cr-main"><header class="cr-topbar"><div class="cr-room-heading">${btn('rooms', icon('menu'), 'aria-label="显示房间列表" aria-expanded="false"', 'cr-icon cr-mobile')}<div class="cr-heading-text"><h1 tabindex="-1" data-cr-title></h1><div class="cr-heading-meta"><span class="cr-chip" data-cr-chip hidden></span><span class="cr-subtitle" data-cr-status></span></div></div></div><nav class="cr-room-actions" aria-label="房间管理">${btn('permissions', icon('folder') + '<span data-cr-project-count>项目</span>')}${btn('members', '<span class="cr-avatars" data-cr-avatars></span><span>成员</span>')}${btn('weixin', '<span class="cr-channel-dot" data-cr-weixin-dot aria-hidden="true"></span>' + icon('message') + '<span data-cr-weixin-label>连接微信</span>')}${btn('settings', '···', 'aria-label="房间设置"', 'cr-icon')}</nav></header>
        <div class="cr-legacy-note" data-cr-legacy hidden><span class="cr-legacy-icon" aria-hidden="true">!</span><div><strong>原房间当前只读</strong><p data-cr-legacy-progress>历史和原工作已保留。处理完旧连接后，由你明确启用新房间界面。</p></div>${btn('migration', '查看迁移要求', 'data-cr-migration-open')}</div>
        <div class="cr-feedback" role="status" aria-live="polite" data-cr-feedback></div>
        <section class="cr-feed" aria-label="房间对话" tabindex="0"><div class="cr-load-older" data-cr-older hidden>${btn('older', '更早的消息')}</div><div data-cr-empty></div><div class="cr-messages" role="log" aria-live="polite" aria-relevant="additions text" data-cr-messages></div><div class="cr-history-end" data-cr-history-end hidden>${btn('latest', '回到最新')}</div><div class="cr-pending" data-cr-pending></div></section><div class="cr-work-bar" data-cr-work hidden></div>
        ${btn('latest', '查看新消息 ↓', 'data-cr-latest hidden', 'cr-latest')}
        <form class="cr-composer" id="cr-composer"><div class="cr-reply cr-context-correction" data-cr-correction hidden></div><div class="cr-reply" data-cr-reply hidden></div><div class="cr-draft-files" data-cr-files aria-label="待发送的附件" aria-live="polite" hidden></div><div class="cr-composer-line"><textarea id="cr-message" name="body_text" aria-label="消息" placeholder="说说你想做什么…" rows="1" maxlength="8000"></textarea><button type="submit" class="cr-send" aria-label="发送消息">${icon('arrow')}</button></div><footer><div class="cr-composer-tools">${btn('attach', icon('plus'), 'aria-label="添加附件" title="每个附件最多 8 MiB"', 'cr-icon cr-attach')}<input type="file" id="cr-file" multiple hidden aria-label="选择附件"><label class="cr-recipient"><span>发送给</span><select name="member_id" aria-label="选择处理成员"><option value="">主责 dot</option></select></label></div><span data-cr-compose-status>Enter 发送 · Shift + Enter 换行</span></footer></form>
      </main><dialog class="cr-panel" aria-labelledby="cr-panel-title"><header><h2 id="cr-panel-title"></h2>${btn('close', icon('close'), 'aria-label="关闭面板"', 'cr-icon')}</header><div class="cr-panel-content"></div><p role="alert" class="cr-error" data-cr-panel-error hidden></p><div class="cr-mutation-recovery" data-cr-panel-recovery hidden></div></dialog>
    </section>`;
  }
  function patchRooms() {
    const list = root?.querySelector('[data-cr-rooms]');
    if (!list) return;
    const needle = roomFilter.trim().toLowerCase();
    const visible = needle
      ? state.rooms.filter((room) =>
          [roomName(room), room.title, ...roomProjects(room)].some((value) =>
            String(value || '')
              .toLowerCase()
              .includes(needle),
          ),
        )
      : state.rooms;
    const count = root.querySelector('[data-cr-room-count]');
    if (count) count.textContent = state.rooms.length ? String(state.rooms.length) : '';
    const markup = visible.length
      ? visible
          .map((room) => {
            const name = roomName(room);
            const current = room.id === state.roomId;
            return `<button type="button" class="cr-room ${current ? 'is-current' : ''} ${room.legacy ? 'is-legacy' : ''}" data-cr-action="select" data-id="${E(room.id)}" aria-current="${current ? 'true' : 'false'}" title="${E(room.title || name)}"><span class="cr-room-avatar" style="--cr-hue:${hue(room.id)}" aria-hidden="true">${E([...name][0] || '房')}</span><span class="cr-room-text"><strong>${E(name)}</strong><small>${E(roomSubtitle(room))}</small></span>${room.legacy ? '<span class="cr-room-flag" aria-label="待迁移">只读</span>' : ''}</button>`;
          })
          .join('')
      : `<p class="cr-list-empty">${needle ? '没有匹配的房间' : '还没有房间'}</p>`;
    if (list._markup !== markup) {
      list.innerHTML = markup;
      list._markup = markup;
    }
    root.querySelector('[data-cr-more-rooms]').hidden = !state.roomsCursor;
    const archives = root.querySelector('[data-cr-action="archives"]');
    archives.textContent = state.includeArchived ? '隐藏已归档' : '显示已归档';
    archives.setAttribute('aria-pressed', String(state.includeArchived));
  }
  function patchHeader() {
    if (!root) return;
    const room = state.room;
    const heading = root.querySelector('[data-cr-title]');
    heading.textContent = room ? roomName(room) : '开始一段协作';
    heading.title = room?.title && room.title !== heading.textContent ? room.title : '';
    const projectNames = room ? roomProjects(room) : [];
    const online = members().filter((member) =>
      ['ready_chat', 'ready_work'].includes(member.stage || member.state),
    ).length;
    root.querySelector('[data-cr-status]').textContent = room
      ? [...projectNames, members().length ? online + '/' + members().length + ' 个 dot 在线' : '']
          .filter(Boolean)
          .join(' · ')
      : '';
    const chip = root.querySelector('[data-cr-chip]'),
      chipValue = roomChip(room);
    chip.hidden = !chipValue || (room && activeRoom());
    if (chipValue) {
      chip.textContent = chipValue.text;
      chip.dataset.tone = chipValue.tone;
    }
    root.querySelector('[data-cr-project-count]').textContent =
      '项目 ' + (room?.projects?.filter((p) => p.state !== 'removed').length || 0);
    root.querySelector('[data-cr-legacy]').hidden = !room?.legacy || !state.messages.length;
    const blockers = room?.migration?.blockers?.length || 0;
    root.querySelector('[data-cr-legacy-progress]').textContent = room?.migration?.eligible
      ? '迁移条件已满足。确认后即可继续协作，历史与原工作保留。'
      : blockers
        ? `还有 ${blockers} 项旧连接待处理。历史与原工作已保留，处理完后由你明确启用新房间界面。`
        : '历史和原工作已保留。处理完旧连接后，由你明确启用新房间界面。';
    const migrationOpen = root.querySelector('[data-cr-migration-open]');
    migrationOpen.textContent = room?.migration?.eligible ? '启用新房间界面' : '查看迁移要求';
    migrationOpen.classList.toggle('cr-primary', !!room?.migration?.eligible);
    const avatars = root.querySelector('[data-cr-avatars]');
    const avatarMarkup = members()
      .slice(0, 3)
      .map((m) => `<i aria-hidden="true">${E((m.name || m.label || 'D').slice(0, 1))}</i>`)
      .join('');
    if (avatars.innerHTML !== avatarMarkup) avatars.innerHTML = avatarMarkup;
    const label = room?.channels?.find((channel) => channel.kind === 'weixin');
    const channelStage = label?.stage || label?.state || '';
    const connected = channelStage === 'ready';
    const attention = ['needs_reconnect', 'degraded', 'pairing_expired', 'route_conflict'].includes(
      channelStage,
    );
    const pending =
      !!channelStage &&
      !connected &&
      !['choose_account', 'cancelled', 'closed', 'disconnected'].includes(channelStage);
    root.querySelector('[data-cr-weixin-label]').textContent = connected
      ? '微信已连接'
      : channelStage === 'paused'
        ? '微信已暂停'
        : attention
          ? '微信需处理'
          : pending
            ? '继续连接微信'
            : '连接微信';
    root.querySelector('[data-cr-weixin-dot]').dataset.state = connected
      ? 'ready'
      : attention
        ? 'attention'
        : pending
          ? 'pending'
          : '';
    for (const node of root.querySelectorAll('.cr-room-actions button')) node.disabled = !room;
    patchRooms();
    patchComposer();
    patchEmpty();
    patchRoomPermissions();
    if (panel?.name === 'migration') renderMigration();
  }
  function patchEmpty() {
    const node = root?.querySelector('[data-cr-empty]');
    if (!node) return;
    const show = !state.messages.length && (!!state.room?.legacy || !state.works.length);
    root.querySelector('[data-cr-legacy]').hidden = !state.room?.legacy || !state.messages.length;
    node.hidden = !show;
    const activeProjectCount = (state.room?.projects || []).filter(
      (p) => p.state !== 'removed',
    ).length;
    const markup = !state.room
      ? `<div class="cr-empty"><div class="cr-orbit" aria-hidden="true"><span></span><i>${icon('message')}</i><span></span></div><h3>还没有房间</h3><p>新建房间后就可以添加项目和 dot。</p>${btn('create', '新建房间', '', 'cr-primary')}</div>`
      : !activeRoom()
        ? state.room.legacy
          ? migrationChecklist()
          : `<div class="cr-empty"><h3>房间当前只读</h3><p>${E(readOnlyReason())}</p>${btn('settings', '查看房间状态')}</div>`
        : `<div class="cr-empty"><div class="cr-orbit" aria-hidden="true"><span></span><i>${icon('network')}</i><span></span></div><h3>${members().length ? '发条消息开始' : '添加一个 dot'}</h3><p>${members().length ? '' : 'dot 是参与房间的助手。也可以先把目标写下来。'}</p><div class="cr-empty-actions">${!members().length ? btn('add-member', '添加 dot', '', 'cr-primary') : ''}${!activeProjectCount ? btn('projects', '添加项目') : ''}</div></div>`;
    if (node._markup !== markup) {
      node.innerHTML = markup;
      node._markup = markup;
    }
  }
  // Legacy rooms: one actionable checklist instead of three copies of the same
  // read-only sentence. Each line carries the exact next step for that blocker.
  function migrationChecklist() {
    const room = state.room,
      migration = room.migration || {},
      blockers = migration.blockers || [],
      counts = migration.member_counts;
    const groups = [
      [
        '成员连接',
        blockers.filter((b) => b.code?.startsWith('MEMBER_') || b.code === 'ROOM_MEMBER_LIMIT'),
      ],
      ['旧 dot 路线', blockers.filter((b) => b.code === 'LEGACY_ROUTE_ACTIVE')],
      ['微信 / Telegram', blockers.filter((b) => b.code === 'LEGACY_CHANNEL_ACTIVE')],
      ['房间状态', blockers.filter((b) => ['ROOM_PAUSED', 'ROOM_ARCHIVED'].includes(b.code))],
    ];
    const steps = groups
      .map(
        ([label, items]) =>
          `<li class="${items.length ? 'is-open' : 'is-done'}"><span class="cr-check" aria-hidden="true">${items.length ? items.length : '✓'}</span><div><strong>${E(label)}</strong>${
            items.length
              ? `<ul>${items
                  .slice(0, 6)
                  .map((item) => migrationBlockerMarkup(item, true))
                  .join(
                    '',
                  )}${items.length > 6 ? `<li>另有 ${items.length - 6} 项，打开迁移要求查看全部</li>` : ''}</ul>`
              : '<p>已处理</p>'
          }</div></li>`,
      )
      .join('');
    const progress = counts
      ? `<p class="cr-muted">原成员 ${E(supplied(counts.total))} 个 · 已验证 ${E(supplied(counts.verified))} · 已移除 ${E(supplied(counts.removed))}</p>`
      : '';
    return `<div class="cr-migrate-card"><div class="cr-migrate-head"><span class="cr-legacy-icon" aria-hidden="true">!</span><div><h3>先核对原房间的连接</h3><p>这个房间来自旧版协作中心。处理完下面的条目并启用新界面后，就能继续发消息、加 dot 和连接微信；历史、原工作和回执都会保留。</p>${progress}</div></div>${migration.eligibility_checked === false ? '<p class="cr-info">迁移条件尚未核对。</p>' : `<ol class="cr-migrate-steps">${steps}</ol>`}<div class="cr-empty-actions">${migration.eligible ? btn('migration', '启用新房间界面', '', 'cr-primary') : btn('migration', '查看迁移要求')}${btn('refresh-migration', '重新核对', '', 'cr-text-button')}</div></div>`;
  }
  function patchComposer() {
    const editor = root?.querySelector('#cr-message'),
      select = root?.querySelector('[name="member_id"]');
    if (!editor) return;
    const draft = store.draft();
    const disabled = !activeRoom();
    root.querySelector('#cr-composer').classList.toggle('is-locked', disabled && !!state.room);
    editor.disabled = disabled;
    if (select) select.disabled = disabled;
    const unfinished = draft.files.some((entry) => entry.state !== 'ready');
    const staleCorrection = contextCorrectionStale();
    const duplicate = [...state.pending.values()].some(
      (receipt) => receipt.roomId === state.roomId && store.sameDraft(receipt),
    );
    root.querySelector('.cr-send').disabled =
      disabled ||
      unfinished ||
      duplicate ||
      staleCorrection ||
      (!editor.value.trim() && !draft.files.length);
    root.querySelector('[data-cr-action="attach"]').disabled = disabled || draft.files.length >= 16;
    root.querySelector('#cr-file').disabled = disabled;
    const status = root.querySelector('[data-cr-compose-status]');
    status.textContent =
      disabled && state.room
        ? state.room.legacy
          ? '迁移前只读 · 草稿已保留'
          : '房间只读 · 草稿已保留'
        : unfinished
          ? '请完成附件上传后发送'
          : draft.contextCorrection
            ? staleCorrection
              ? '请重新核对当前事实'
              : '发送后纠正所选事实'
            : draft.work
              ? '补充到当前工作'
              : 'Enter 发送 · Shift + Enter 换行';
    patchFiles();
    const correction = root.querySelector('[data-cr-correction]');
    correction.hidden = !draft.contextCorrection;
    if (draft.contextCorrection) {
      const markup = `<span>${staleCorrection ? '上下文已变化，请重新选择要纠正的事实。' : '正在纠正事实 ' + E(draft.contextCorrection.ids.map(identifier).join('、')) + ' · 版本 ' + E(draft.contextCorrection.version)}</span>${btn('context', staleCorrection ? '重新核对' : '查看事实', '', 'cr-text-button')}${btn('cancel-context-correction', '×', 'aria-label="取消纠正上下文"', 'cr-icon')}`;
      if (correction._markup !== markup) {
        correction.innerHTML = markup;
        correction._markup = markup;
      }
    }
    if (select && !select.matches(':focus')) {
      const rows = members();
      const names = rows.map((member) => member.name || member.label || 'dot');
      const markup =
        '<option value="">主责 dot</option>' +
        rows
          .map((member, index) => {
            const name = names[index],
              stage = member.stage || member.state;
            const status = memberBadges[stage] || '连接中';
            const dot = status === '在线' ? '●' : status === '需要处理' ? '◐' : '○';
            const duplicate = names.filter((item) => item === name).length > 1;
            return `<option value="${E(memberId(member))}">${dot} ${E(name)}${duplicate ? ' · ' + E(memberShort(member)) : ''}</option>`;
          })
          .join('');
      if (select._markup !== markup) {
        select.innerHTML = markup;
        select._markup = markup;
      }
      select.value = draft.member;
    }
    const reply = root.querySelector('[data-cr-reply]');
    reply.hidden = !draft.reply && !draft.work;
    if (draft.reply || draft.work) {
      const original = state.messages.find((m) => m.id === draft.reply);
      const work = state.works.find((item) => item.id === draft.work);
      const markup = `<span>${draft.work ? '补充工作：' + E(work?.title || '当前工作') : '回复：' + E((original?.body_text || '原消息').slice(0, 100))}</span>${btn(draft.work ? 'cancel-supplement' : 'cancel-reply', '×', `aria-label="${draft.work ? '取消补充工作' : '取消引用'}"`, 'cr-icon')}`;
      if (reply._markup !== markup) {
        reply.innerHTML = markup;
        reply._markup = markup;
      }
    }
  }
  function fileSize(value) {
    return value >= 1024 * 1024
      ? (value / (1024 * 1024)).toFixed(1) + ' MiB'
      : value >= 1024
        ? Math.ceil(value / 1024) + ' KiB'
        : value + ' B';
  }
  function patchFiles() {
    const box = root?.querySelector('[data-cr-files]');
    if (!box) return;
    const files = store.draft().files;
    box.hidden = !files.length;
    const markup = files
      .map(
        (entry) =>
          `<article class="cr-file" data-file-id="${E(entry.id)}" data-state="${E(entry.state)}"><div><strong>${E(entry.attachment?.name || entry.name)}</strong><small>${fileSize(entry.size)} · ${entry.state === 'ready' ? '已上传，待发送' : entry.state === 'unknown' ? '上传结果待核对' : entry.state === 'failed' ? E(entry.error || '上传失败') : '正在上传…'}</small></div>${['failed', 'unknown'].includes(entry.state) ? btn('retry-file', entry.state === 'unknown' ? '确认是否已上传' : '重试', `data-id="${E(entry.id)}" ${activeRoom() ? '' : 'disabled'}`, 'cr-text-button') : ''}${btn('remove-file', '×', `data-id="${E(entry.id)}" aria-label="移除附件 ${E(entry.name)}"`, 'cr-icon')}</article>`,
      )
      .join('');
    if (box._markup !== markup) {
      box.innerHTML = markup;
      box._markup = markup;
    }
  }
  function attachmentMarkup(item) {
    const expired =
      item.state === 'expired' ||
      (item.retention?.expires_at && item.retention.expires_at * 1000 <= Date.now());
    const downloadable =
      !expired &&
      item.state === 'ready' &&
      /^\/api\/collaboration\/attachments\/[a-zA-Z0-9_-]+\/content(?:\?space_id=[a-zA-Z0-9_%.-]+)?$/.test(
        item.download_path || '',
      );
    const body = `<span>${E(item.name || '附件')}</span><small>${fileSize(item.size || 0)} · ${expired ? '已过保留期限' : downloadable ? '下载原文件' : '文件暂不可用'}</small>`;
    return downloadable
      ? `<a class="cr-message-file" href="${E(item.download_path)}" download="${E(item.name || 'attachment')}">${body}</a>`
      : `<span class="cr-message-file is-unavailable">${body}</span>`;
  }
  function messageProgress(message) {
    const route = message.routing || {};
    const progress = message.display || {};
    const reason = {
      member_paused: '已保存，等待 dot 恢复',
      member_not_connected: '已保存，等待 dot 恢复',
      member_removed: '已保存，等待原 dot 恢复',
      lead_member_required: '已保存，等待主责',
      source_unavailable: '原消息或微信连接已失效',
      work_unavailable: '原工作已结束或不可继续',
    }[route.reason_code];
    const label =
      (progress.state && progress.title) ||
      (route.state !== 'queued' && reason) ||
      (route.state === 'queued' && '待 dot 处理') ||
      progress.title;
    if (!label) return '';
    return `<span data-cr-message-progress data-state="${E(progress.state || route.state || 'saved')}"${progress.reason ? ` title="${E(progress.reason)}"` : ''}>${E(label)}</span>`;
  }
  function messageMarkup(message) {
    const owner = ['owner', 'user', 'human'].includes(message.author_kind);
    return `<header><strong>${E(message.author_name || message.display_name || message.member_name || (owner ? '你' : message.author_kind === 'system' ? '房间动态' : 'dot'))}</strong><time>${E(time(message.created_at || message.created))}</time></header>${message.reply_to_id ? '<span class="cr-message-reference">回复原消息</span>' : ''}<div class="cr-message-body" ${message.body_text ? '' : 'hidden'}></div>${message.attachments?.length ? `<div class="cr-message-files">${message.attachments.map(attachmentMarkup).join('')}</div>` : ''}${deliveryMarkup(message.channel_deliveries)}<div data-cr-completed-work hidden></div><footer>${messageProgress(message)}${btn('reply', '回复', `data-id="${E(message.id)}"`, 'cr-text-button')}</footer>`;
  }
  function deliveryReason(code) {
    return (
      {
        WEIXIN_RATE_LIMITED: '微信暂时限制发送',
        WEIXIN_REQUEST_LIMIT: '消息或附件超出微信发送上限',
        attachment_unavailable: '原附件暂不可用',
        daily_reply_quota: '已达到今日回传额度',
        context_missing: '等待原微信私聊的会话上下文',
        dependency_failed: '同一回复的前一段发送失败',
        source_unavailable: '原消息或微信连接已失效',
        provider_response_unknown: '微信未返回可核对的发送结果',
        authorization_revoked: '工作许可已撤销',
        authorization_expired: '工作许可已到期',
        scope_changed: '原工作范围已变化',
        historical_ordering_unverified: '无法确认旧回复的顺序关系，暂时保留顺序等待',
      }[code] || reasonText(code)
    );
  }
  function deliveryMarkup(deliveries = [], display, truncated = false) {
    if (!deliveries?.length) return '';
    return `<section class="cr-deliveries" aria-label="发送状态" data-cr-deliveries>${deliveries
      .map((delivery) => {
        const parts = delivery.parts || [];
        const verified =
          delivery.read_verified === true || delivery.provider_delivery_verified === true;
        const uncertain =
          delivery.state === 'unknown' || parts.some((part) => part.state === 'unknown');
        const manuallyConfirmed =
          parts.some((part) => part.state === 'owner_confirmed') &&
          parts.every((part) => ['accepted', 'owner_confirmed'].includes(part.state));
        const state = verified
          ? '已送达'
          : uncertain
            ? '送达待核对'
            : manuallyConfirmed
              ? '房主已确认收到'
              : deliveryLabels[delivery.state] || '状态待核对';
        const name = delivery.channel === 'weixin' ? '微信' : delivery.channel || '渠道';
        const label =
          state === '已受理' || state === '已送达' ? name + state : name + ' · ' + state;
        const detail =
          uncertain ||
          manuallyConfirmed ||
          !!delivery.ordering_reason ||
          ['发送失败', '已取消'].includes(state);
        const retry =
          activeRoom() &&
          delivery.channel === 'weixin' &&
          Number.isInteger(delivery.version) &&
          (delivery.parts || []).some((part) => part.state === 'attention' && part.attempts === 0);
        return `<div class="cr-delivery" data-delivery-id="${E(delivery.id)}"><span>${E(label)}${delivery.kind === 'status' ? ' · 状态通知' : ''}</span>${retry ? btn('retry-delivery', '重试', 'data-id="' + E(delivery.id) + '" data-version="' + E(delivery.version) + '"', 'cr-text-button') : ''}${
          detail
            ? `<details><summary>详情</summary>
        ${delivery.display?.reason ? `<p>${E(delivery.display.reason)}</p>` : ''}
        ${delivery.ordering_reason ? `<p>${E(deliveryReason(delivery.ordering_reason))}</p>` : ''}
        <ul>${parts.map((part) => `<li data-delivery-part="${E(part.id || part.ordinal)}" data-state="${E(part.state)}">${E({ text: '文字', image: '图片', file: '文件', voice: '语音', video: '视频' }[part.kind] || '消息')} · ${E(deliveryLabels[part.state] || '状态待核对')}${part.reason_code && part.state !== 'owner_confirmed' ? `<p>${E(deliveryReason(part.reason_code))}</p>` : ''}</li>`).join('')}</ul>
        </details>`
            : ''
        }</div>`;
      })
      .join(
        '',
      )}${truncated && display?.reason ? `<p class="cr-muted">${E(display.reason)}</p>` : ''}</section>`;
  }
  function patchWindowHints() {
    if (!root) return;
    root.querySelector('[data-cr-older]').hidden = !state.cursor;
    const boundary = root.querySelector('[data-cr-history-end]');
    boundary.hidden = !state.messageHistory;
    boundary.style.minHeight = state.messageHistory
      ? root.querySelector('.cr-feed').clientHeight + 'px'
      : '';
    if (state.newerMessages) root.querySelector('[data-cr-latest]').hidden = false;
  }
  function patchMessageContent(node, message) {
    const expanded = new Set(
      [...node.querySelectorAll('[data-delivery-id] > details[open]')].map(
        (item) => item.parentElement.dataset.deliveryId,
      ),
    );
    const template = document.createElement('div');
    template.innerHTML = messageMarkup(message);
    let body = node.querySelector('.cr-message-body');
    if (!body) {
      body = document.createElement('div');
      body.className = 'cr-message-body';
      node.append(body);
    }
    for (const child of [...node.children]) if (child !== body) child.remove();
    let before = true;
    for (const child of [...template.children]) {
      if (child.classList.contains('cr-message-body')) {
        before = false;
        continue;
      }
      if (before) node.insertBefore(child, body);
      else node.append(child);
    }
    const text = message.body_text || '';
    body.hidden = !text;
    if (body._crText !== text) {
      if (typeof chatRichMarkdown === 'function') chatRichMarkdown(body, text);
      else body.textContent = text;
      body._crText = text;
    }
    for (const details of node.querySelectorAll('[data-delivery-id] > details'))
      if (expanded.has(details.parentElement.dataset.deliveryId)) details.open = true;
  }
  function patchMessages() {
    if (!root) return;
    const list = root.querySelector('[data-cr-messages]'),
      feed = root.querySelector('.cr-feed');
    const nearEnd = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
    const reading = readingSnapshot();
    let changed = false;
    const visibleIds = new Set(state.messages.map((message) => message.id));
    for (const node of [...list.children])
      if (!visibleIds.has(node.dataset.messageId)) {
        node.remove();
        changed = true;
      }
    for (const message of state.messages) {
      let node = [...list.children].find((child) => child.dataset.messageId === message.id);
      if (!node) {
        node = document.createElement('article');
        node.dataset.messageId = message.id;
        list.append(node);
        changed = true;
      }
      const signature = JSON.stringify([
        message,
        (message.attachments || []).map(
          (file) =>
            !!(file.retention?.expires_at && file.retention.expires_at * 1000 <= Date.now()),
        ),
      ]);
      if (node._signature === signature) continue;
      node._signature = signature;
      node.className =
        'cr-message ' +
        (['owner', 'user', 'human'].includes(message.author_kind) ? 'is-owner' : 'is-dot');
      patchMessageContent(node, message);
      changed = true;
    }
    // Stable nodes keep browser selection and scroll intact across projections.
    state.messages.forEach((message, index) => {
      const node = [...list.children].find((child) => child.dataset.messageId === message.id);
      if (node && list.children[index] !== node)
        list.insertBefore(node, list.children[index] || null);
    });
    patchWindowHints();
    patchRoomPermissions();
    if (changed && !root._restoringReading && !root._loadingOlder) restoreAnchor(reading);
    patchEmpty();
    if (
      changed &&
      nearEnd &&
      !state.messageHistory &&
      !root._loadingOlder &&
      !root._restoringReading
    ) {
      const top = feed.scrollTop,
        epoch = state.epoch;
      requestAnimationFrame(() => {
        if (!feed.isConnected || epoch !== state.epoch || root?._restoringReading) return;
        if (Math.abs(feed.scrollTop - top) < 2) feed.scrollTop = feed.scrollHeight;
        else if (feed.scrollHeight - feed.scrollTop - feed.clientHeight >= 90)
          root.querySelector('[data-cr-latest]').hidden = false;
      });
    } else if (changed && !root._loadingOlder && !root._restoringReading)
      root.querySelector('[data-cr-latest]').hidden = false;
  }
  function patchPending() {
    const node = root?.querySelector('[data-cr-pending]');
    if (!node) return;
    const receipts = [...state.pending.values()].filter((item) => item.roomId === state.roomId);
    const markup =
      receipts
        .map(
          (item) =>
            `<article class="cr-pending-item"><p>${E(item.body.text)}${item.body.attachments?.length ? `<small>${E(item.body.attachments.map((file) => file.name).join('、'))}</small>` : ''}</p><div>${item.state === 'sending' ? '正在发送…' : item.state === 'unknown' ? '正在核对是否已保存' : E(item.error || '发送失败')}${item.state !== 'sending' ? btn('recover-send', item.state === 'unknown' ? '确认是否已发送' : '继续发送', `data-id="${E(item.id)}" ${item.state === 'unknown' || activeRoom() ? '' : 'disabled'}`) : ''}</div></article>`,
        )
        .join('') + mutationMarkup();
    if (node._markup !== markup) {
      node.innerHTML = markup;
      node._markup = markup;
    }
    patchMutationRecovery();
  }
  function mutationMarkup() {
    const migrationComplete =
      state.room?.migration?.status === 'complete' || state.room?.migration?.activated_at != null;
    return [...state.mutations.values()]
      .filter((item) => item.roomId === state.roomId || item.kind === 'create')
      .filter(
        (item) =>
          !(
            item.kind === 'migration' &&
            item.state === 'not_saved' &&
            item.roomId === state.room?.id &&
            migrationComplete
          ),
      )
      .map(
        (item) =>
          `<article class="cr-pending-item" data-mutation-state="${E(item.state)}"><p>${E(item.label)} · ${item.state === 'not_saved' ? '原请求未保存' : '结果尚未确认'}${item.state !== 'not_saved' && !mutationAllowed(item.method, item.url, JSON.parse(item.body), item.roomId) ? '<small>仅按原请求键核对。若原操作未保存，当前只读状态会阻止执行；不会改用新的请求。</small>' : ''}${item.error ? `<small>${E(item.error)}</small>` : ''}</p><div>${item.state === 'not_saved' ? btn('migration', '重新核对并确认') : item.state === 'recovering' ? '正在核对原请求…' : btn('recover-action', '查看结果', `data-id="${E(item.id)}"`)}</div></article>`,
      )
      .join('');
  }
  function patchMutationRecovery() {
    const box = root?.querySelector('[data-cr-panel-recovery]');
    if (!box) return;
    const markup = mutationMarkup();
    box.hidden = !markup;
    if (box._markup !== markup) {
      box.innerHTML = markup;
      box._markup = markup;
    }
  }
  function patchWorks() {
    const node = root?.querySelector('[data-cr-work]');
    if (!node) return;
    const waiting = state.works.filter((work) => workLabels[work.state] === '等你回复').length;
    const active = state.works.filter((work) => !finishedWork(work)).length;
    const markup = btn(
      'work-list',
      '进行中 ' + (active - waiting) + (waiting ? ' · 等你回复 ' + waiting : ''),
      '',
      'cr-text-button',
    );
    node.hidden = !state.works.length && state.worksComplete && !state.worksCursor;
    if (node._markup !== markup) {
      node.innerHTML = markup;
      node._markup = markup;
    }
    if (panel?.name === 'work-list') renderWorkList();
    for (const message of root.querySelectorAll('[data-cr-completed-work]')) {
      const works = state.works.filter(
        (work) =>
          ['completed', 'succeeded'].includes(work.state) &&
          (work.source_message_id || work.intent?.source_message_id) ===
            message.closest('[data-message-id]')?.dataset.messageId,
      );
      const items = works
        .map((work) =>
          btn(
            'work-detail',
            '已完成：' + E(work.title || '工作'),
            'data-id="' + E(work.id) + '"',
            'cr-text-button',
          ),
        )
        .join('');
      if (message._markup !== items) {
        message.innerHTML = items;
        message._markup = items;
      }
      message.hidden = !items;
    }
    patchEmpty();
  }
  function renderWorkList() {
    const list = root?.querySelector('[data-cr-work-list-view]');
    if (!list) return;
    const ordered = [...state.works].sort(
      (a, b) => Number(finishedWork(a)) - Number(finishedWork(b)),
    );
    const markup =
      ordered
        .map(
          (work) => `<article class="cr-work" data-work-id="${E(work.id)}">
      <span class="cr-work-indicator ${E(work.state)}" aria-hidden="true"></span><div><strong>${E(work.title || '工作')}</strong>
      <p>${E(workLabels[work.state] || work.display?.title || work.state)}${work.display?.reason ? ' · ' + E(work.display.reason) : ''}</p></div>
      ${btn('work-detail', '查看', 'data-id="' + E(work.id) + '"', 'cr-text-button')}</article>`,
        )
        .join('') +
      (state.worksCursor ? btn('more-work', '更早的工作', '', 'cr-full') : '') +
      (state.workHistory ? btn('latest-work', '回到最新', '', 'cr-full') : '');
    if (list._markup !== markup) {
      list.innerHTML = markup || '<p>暂无工作。</p>';
      list._markup = markup;
    }
  }
  function update(domain) {
    if (!root) return;
    if (['room', 'rooms'].includes(domain)) patchHeader();
    if (domain === 'room') {
      patchPending();
      if (panel?.name === 'work' && panel.work) renderWork(panel.work);
    }
    if (domain === 'context') patchComposer();
    if (['room', 'context'].includes(domain) && panel?.name === 'context' && panel.context) {
      const latest = state.room?.context;
      if (
        latest &&
        (latest.version !== panel.context.version ||
          latest.visibility_revision !== panel.context.visibility_revision)
      ) {
        panel.context = latest;
        renderContext();
        panelError('当前上下文已更新，旧页已清除，请按最新事实核对。');
      }
    }
    if (domain === 'messages') {
      patchMessages();
      patchWorks();
    }
    if (domain === 'works') {
      patchWorks();
      if (panel?.name === 'work' && !panel.busy) {
        const latest =
          state.works.find((work) => work.id === panel.work.id) ||
          (state.workDetail?.id === panel.work.id ? state.workDetail : null);
        if (latest) renderWork(latest);
      }
    }
    if (domain === 'pending') {
      patchPending();
      patchComposer();
    }
    if (domain === 'draft') patchComposer();
    if (domain === 'sent') {
      const editor = root.querySelector('#cr-message');
      editor.value = store.draft().text;
      sizeEditor(editor);
      patchComposer();
      patchPending();
    }
    if (domain === 'page') patchWindowHints();
  }
  function sizeEditor(editor) {
    editor.style.height = 'auto';
    editor.style.height = Math.min(editor.scrollHeight, 180) + 'px';
  }
  function closePanel({ restoreFocus = true } = {}) {
    const dialog = root?.querySelector('dialog');
    if (!dialog) return;
    if (!restoreFocus) dialog._crOpener = null;
    panel?.impactController?.abort();
    if (panel?.name === 'work') state.workDetail = null;
    panelEpoch++;
    panel = null;
    CP.ui.closeModalLayer(dialog);
  }
  function openPanel(name, heading, content, data = {}) {
    panel?.impactController?.abort();
    if (panel?.name === 'work') state.workDetail = null;
    panelEpoch++;
    panel = { name, roomId: state.roomId, epoch: panelEpoch, key: store.key(), ...data };
    const dialog = root.querySelector('dialog');
    dialog._crOpeningEpoch = panel.epoch;
    if (!dialog.open)
      dialog._crOpener = root._panelOpener?.isConnected
        ? root._panelOpener
        : document.activeElement;
    dialog.querySelector('h2').textContent = heading;
    dialog.querySelector('.cr-panel-content').innerHTML = content;
    dialog.scrollTop = 0;
    panelError('');
    patchMutationRecovery();
    patchRoomPermissions();
    if (!dialog.open) CP.ui.showModalLayer(dialog);
    const openingEpoch = panel.epoch;
    requestAnimationFrame(() => {
      if (!dialog.open || panel?.epoch !== openingEpoch) return;
      const focused = document.activeElement;
      if (focused && focused !== dialog && dialog.contains(focused)) return;
      const controls = CP.ui.focusable(dialog);
      (controls.find((control) => control.matches('[autofocus]')) || controls[0])?.focus();
    });
  }
  function patchRoomPermissions() {
    if (!root) return;
    const restricted = !activeRoom();
    const disable = (control, blocked) => {
      if (blocked) {
        if (control._crPermissionDisabled === undefined)
          control._crPermissionDisabled = control.disabled;
        control.disabled = true;
      } else if (control._crPermissionDisabled !== undefined) {
        control.disabled = control._crPermissionDisabled;
        delete control._crPermissionDisabled;
      }
    };
    for (const form of root.querySelectorAll('dialog [data-cr-form]')) {
      const kind = form.dataset.crForm;
      const business =
        ['permissions', 'member', 'work-recovery'].includes(kind) ||
        (kind === 'weixin' && !safeChannelActions.has(form.dataset.channelAction));
      if (!business) continue;
      for (const control of form.querySelectorAll('input,select,textarea,button'))
        disable(control, restricted);
    }
    for (const button of root.querySelectorAll('[data-cr-action]')) {
      const action = button.dataset.crAction;
      const business =
        [
          'add-member',
          'lead',
          'supplement',
          'reply',
          'correct-context',
          'channel-existing',
          'connection-renewal',
        ].includes(action) ||
        (action === 'work-control' && !safeWorkActions.has(button.dataset.workAction)) ||
        (action === 'channel-action' && !safeChannelActions.has(button.dataset.channelAction));
      if (business) disable(button, restricted);
    }
    const migrationForm = root.querySelector('[data-cr-form="migration"]');
    if (migrationForm)
      for (const control of migrationForm.querySelectorAll('input,button'))
        control.disabled =
          panel?.busy || !state.room?.legacy || state.room?.migration?.eligible !== true;
  }
  function migrationRevision(room = state.room) {
    return JSON.stringify([room?.version, room?.legacy, room?.migration]);
  }
  function migrationPanel() {
    if (!state.room) return;
    openPanel('migration', '启用新房间界面', '<div data-cr-migration-view></div>');
    renderMigration();
  }
  function legacyChannelRemediation(blocker) {
    if (
      blocker.code !== 'LEGACY_CHANNEL_ACTIVE' ||
      !blocker.binding_id ||
      !['weixin', 'telegram'].includes(blocker.channel) ||
      !Number.isInteger(blocker.binding_version)
    )
      return null;
    const expectedPath =
      store.path() + '/legacy-channels/' + encodeURIComponent(blocker.binding_id) + '/pause';
    const remediation = blocker.remediation;
    return remediation?.method === 'POST' &&
      remediation.path === expectedPath &&
      remediation.body?.channel === blocker.channel &&
      remediation.body.expected_version === blocker.binding_version
      ? remediation
      : null;
  }
  function migrationBlockerMarkup(blocker, inline = false) {
    const memberAction =
      blocker.member_id && members().some((member) => memberId(member) === blocker.member_id)
        ? btn(
            inline ? 'open-member' : 'member-detail',
            '管理此成员',
            `data-id="${E(blocker.member_id)}"`,
            'cr-text-button',
          )
        : '';
    const roomAction = ['ROOM_PAUSED', 'ROOM_ARCHIVED'].includes(blocker.code)
      ? btn('settings', '查看房间状态', '', 'cr-text-button')
      : '';
    const channelAction = legacyChannelRemediation(blocker)
      ? btn(
          inline ? 'inline-channel-pause' : 'legacy-channel-pause',
          blocker.channel === 'weixin' ? '暂停原微信连接' : '暂停原 Telegram 连接',
          `data-id="${E(blocker.binding_id)}"`,
          'cr-text-button',
        )
      : '';
    // The migration dialog owns data-migration-blocker; summaries elsewhere
    // (room checklist, WeChat wizard) use a separate hook.
    return `<li ${inline ? 'data-migration-item' : 'data-migration-blocker'}="${E(blocker.code)}"><span>${E(blocker.title || blocker.code)}</span>${memberAction}${roomAction}${channelAction}</li>`;
  }
  function renderMigration() {
    if (panel?.name !== 'migration') return;
    const box = root?.querySelector('[data-cr-migration-view]');
    if (!box || !state.room) return;
    const room = state.room,
      migration = room.migration || {},
      revision = migrationRevision(room),
      changed = panel.migrationRevision && panel.migrationRevision !== revision;
    panel.version = room.version;
    panel.migrationRevision = revision;
    if (changed) panel.key = '';
    const eligible = room.legacy && migration.eligible === true;
    const counts = migration.member_counts;
    const markup = room.legacy
      ? `<p class="cr-info">${E(readOnlyReason())}</p><p>逐一迁移或移除原成员，并安全暂停旧的消息路线。成员验证完成后，仍需在这里明确启用。</p>${counts ? `<dl class="cr-migration-counts"><div><dt>原成员</dt><dd>${E(supplied(counts.total))}</dd></div><div><dt>已验证</dt><dd>${E(supplied(counts.verified))}</dd></div><div><dt>已移除</dt><dd>${E(supplied(counts.removed))}</dd></div><div><dt>待处理</dt><dd>${E(supplied(counts.pending))}</dd></div></dl>` : ''}${migration.blockers_truncated ? '<p class="cr-info">当前只列出有界迁移条件，处理后请重新核对剩余连接。</p>' : ''}<div data-cr-migration-blockers>${migration.blockers?.length ? `<ul>${migration.blockers.map(migrationBlockerMarkup).join('')}</ul>` : `<p>${eligible ? '可以启用新版房间。' : '迁移条件尚未确认，请重新读取。'}</p>`}</div><div class="cr-setting-actions">${btn('members', '查看原成员')}${btn('diagnostics', '查看运行诊断')}${btn('refresh-migration', '重新核对')}</div><form data-cr-form="migration"><button type="submit" class="cr-button cr-primary cr-full" ${eligible ? '' : 'disabled'}>启用新房间界面</button></form>`
      : '<p class="cr-info">新房间界面已启用。原历史和工作已保留，后续动作仍按当前批准范围核验。</p>';
    if (box._markup !== markup || changed) {
      box.innerHTML = markup;
      box._markup = markup;
    }
    if (changed && room.legacy) panelError('房间或迁移条件已变化，请重新核对并确认。');
  }
  function createPanel() {
    const projects = (S.projects || [])
      .map(
        (p) =>
          `<label class="cr-choice"><input type="checkbox" name="project" value="${E(p.id)}"><span>${E(p.alias)}</span></label>`,
      )
      .join('');
    openPanel(
      'create',
      '新建房间',
      `<form data-cr-form="create">${field('房间名称', '<input name="title" required maxlength="120" placeholder="例如：产品工作室" autofocus>')}
        ${projects ? `<fieldset class="cr-create-projects"><legend>项目（可选，初始只读）</legend>${projects}</fieldset>` : ''}
        <button class="cr-button cr-primary" type="submit">创建房间</button></form>`,
    );
  }
  const permissionCaps = ['read', 'write', 'execute'];
  const permissionLevels = {
    '': ['不加入', []],
    read: ['只读', ['read']],
    write: ['读写', ['read', 'write']],
    run: ['读取和运行', ['read', 'execute']],
    execute: ['读写和运行', ['read', 'write', 'execute']],
  };
  const permissionLevel = (caps) =>
    caps.includes('execute')
      ? caps.includes('write')
        ? 'execute'
        : 'run'
      : caps.includes('write')
        ? 'write'
        : caps.includes('read')
          ? 'read'
          : '';
  const permissionOptions = (value = '', bulk = false) =>
    Object.entries(permissionLevels)
      .filter(([key]) => !bulk || key !== 'run')
      .map(
        ([key, [label]]) =>
          `<option value="${key}" ${key === value ? 'selected' : ''}>${label}</option>`,
      )
      .join('');
  const permissionKey = (value) =>
    JSON.stringify([value.project_id, value.environment_id || 'production']);
  function permissionDateValue(timestamp) {
    if (!Number.isFinite(Number(timestamp)) || timestamp == null) return '';
    const value = new Date(Number(timestamp) * 1000);
    return new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  }
  function permissionLifetimeControls(lifetime, inherit) {
    const value = lifetime || { mode: 'until_revoked', expires_at: null };
    return `<div data-permission-lifetime data-original-lifetime="${E(JSON.stringify(value))}" data-inherit-lifetime="${inherit}">${field('有效期', `<select data-permission-lifetime-mode><option value="until_revoked" ${value.mode === 'until_revoked' ? 'selected' : ''}>直到我撤销</option><option value="timed" ${value.mode === 'timed' ? 'selected' : ''}>指定截止时间</option></select>`)}${field('截止时间（本地时间）', `<input type="datetime-local" step="1" data-permission-lifetime-until value="${E(permissionDateValue(value.expires_at))}">`)}<small class="cr-warning" data-permission-expired hidden>已过期：请在这里选择新的截止时间或改为直到我撤销，再保存续期。</small>${inherit ? '<small class="cr-muted">新自定义范围默认继承当前房间项目有效期；不能晚于房间截止时间。</small>' : ''}</div>`;
  }
  function permissionLifetimeValue(group) {
    const controls = group.querySelector('[data-permission-lifetime]');
    const original = JSON.parse(controls.dataset.originalLifetime);
    const displayed = controls.querySelector('[data-permission-lifetime-until]').value;
    const normalized = displayed.length === 16 ? displayed + ':00' : displayed;
    if (
      !controls.dataset.lifetimeEdited &&
      controls.querySelector('[data-permission-lifetime-mode]').value === original.mode &&
      normalized === permissionDateValue(original.expires_at)
    )
      return original;
    const mode = controls.querySelector('[data-permission-lifetime-mode]').value;
    const value = controls.querySelector('[data-permission-lifetime-until]').value;
    return {
      mode,
      expires_at: mode === 'timed' && value ? new Date(value).getTime() / 1000 : null,
    };
  }
  function permissionRows(policy, scope, section) {
    const prior = scope?.project_scopes || [];
    return (
      (policy.available_projects || [])
        .map((project) => {
          const rule = prior.find((item) => permissionKey(item) === permissionKey(project));
          const parent = (policy.scope?.project_scopes || []).find(
            (item) => permissionKey(item) === permissionKey(project),
          );
          const lifetime = rule?.lifetime || (section !== 'room' ? parent?.lifetime : null);
          const selected = rule?.capabilities || [];
          const available = project.capabilities || [];
          const targets =
            project.execution_targets || (available.includes('execute') ? ['project_agent'] : []);
          const selectedTargets = selected.includes('execute')
            ? rule.execution_targets || []
            : targets;
          const label = project.name || project.project_label || '项目';
          const env = project.environment_id || 'production';
          return `<div class="cr-scope" data-permission-row data-project-id="${E(project.project_id)}" data-environment-id="${E(env)}" data-project-label="${E(label)}">
            <div class="cr-permission-row-main">
              <label class="cr-choice cr-permission-project"><input type="checkbox" data-permission-project ${selected.length ? 'checked' : ''}><span>${E(label)}${env === 'production' ? '' : ' · ' + E(env)}</span></label>
              <select data-permission-level aria-label="${E(label)}的权限" title="可选权限受项目和电脑的设置限制">${permissionOptions(permissionLevel(selected))}</select>
            </div>
            <div hidden>${permissionCaps.map((cap) => `<input type="checkbox" data-permission-cap value="${cap}" data-host-available="${available.includes(cap)}" ${selected.includes(cap) ? 'checked' : ''}>`).join('')}</div>
            ${targets.length ? `<details data-permission-targets><summary>运行位置</summary>${targets.map((target) => `<label class="cr-choice"><input type="checkbox" data-permission-target-option value="${E(target)}" ${selectedTargets.includes(target) ? 'checked' : ''}><span>${E(project.execution_target_labels?.[target] || (target === 'project_agent' ? '项目电脑' : target))}</span></label>`).join('')}</details>` : ''}
            <details class="cr-permission-expiry"><summary>有效期</summary>${permissionLifetimeControls(lifetime, section !== 'room' && !rule)}</details>
          </div>`;
        })
        .join('') || '<p class="cr-muted">没有可授权项目。</p>'
    );
  }
  function permissionSection(policy, scope, section) {
    return `<section data-permission-section="${section}"><div class="cr-permission-bulk">
      <label class="cr-choice cr-permission-select-all"><input type="checkbox" data-permission-select-all><span>全选</span><span class="cr-muted" data-permission-count aria-live="polite"></span></label>
      <label>所选权限 <select data-permission-bulk-level><option value="-" selected>选择权限</option>${permissionOptions('-', true)}</select></label>
      ${section === 'room' ? '<label>所选项目有效期 <select data-permission-global-expiry><option value="">各项目设置</option><option value="until_revoked">直到撤销</option><option value="timed">指定时间</option></select></label><label data-permission-global-date hidden>截止时间 <input type="datetime-local"></label>' : ''}
      </div>${permissionRows(policy, scope, section)}</section>`;
  }
  function renderPermissions(policy) {
    if (panel?.name !== 'permissions') return;
    panel.permissions = policy;
    panel.version = policy.version;
    panel.stale = false;
    panel.continuationPreview = null;
    panel.continuationSelection = null;
    const channels = ['weixin', 'telegram'].filter(
      (name) => policy.channels?.[name]?.transport_supported !== false,
    );
    root.querySelector('[data-cr-permissions-view]').innerHTML =
      `${panel.returnChannel ? btn('channel-scope-back', '返回微信', '', 'cr-text-button') : ''}<p class="cr-muted">保存后生效。</p>${activeRoom() ? '' : `<p class="cr-info">${E(readOnlyReason())}</p>`}
      <form data-cr-form="permissions"><h3>项目权限</h3>${permissionSection(policy, policy.scope, 'room')}
      ${channels
        .map((channel) => {
          const entry = policy.channels?.[channel] || { mode: 'follow_room', scope: {} };
          const name = channel === 'weixin' ? '微信' : 'Telegram';
          return `<section class="cr-inspection-section"><label class="cr-choice"><input type="checkbox" data-permission-mode="${channel}" ${entry.mode === 'follow_room' ? 'checked' : ''}><span>${name}使用和房间相同的权限</span></label><div data-permission-custom="${channel}" ${entry.mode === 'custom' ? '' : 'hidden'}>${permissionSection(policy, entry.scope, channel)}</div></section>`;
        })
        .join('')}
      <section class="cr-continuation" aria-label="未完成的任务">
        <label class="cr-choice"><input type="checkbox" data-cr-continue-selected><span>同时继续未完成的任务<small>只继续本次清单中你选中的任务。已完成的步骤沿用原回执。</small></span></label>
        <div data-cr-continuation-preview hidden></div>
      </section>
      <button class="cr-button cr-primary" type="submit">保存权限</button></form><div data-cr-continuation-result></div>${btn('reload-permissions', '放弃修改', '', 'cr-text-button')}`;
    updatePermissionControls();
    renderContinuationResult(panel);
  }
  async function permissionsPanel(returnChannel = null) {
    if (!state.room) return;
    openPanel(
      'permissions',
      '权限设置',
      `<div data-cr-permissions-view>${returnChannel ? btn('channel-scope-back', '返回微信', '', 'cr-text-button') : ''}<p class="cr-muted">正在读取房间权限…</p>${btn('reload-permissions', '重新读取', '', 'cr-text-button')}</div>`,
      { returnChannel },
    );
    const current = panel;
    const policy = await api(store.path(current.roomId) + '/permissions');
    if (panel === current && current.roomId === state.roomId) {
      renderPermissions(policy);
      void refreshContinuationResult(current);
    }
    return current;
  }
  function updatePermissionControls() {
    const form = root?.querySelector('[data-cr-form="permissions"]');
    if (!form) return;
    for (const select of form.querySelectorAll('[data-permission-mode]')) {
      const box = form.querySelector(
        '[data-permission-custom="' + select.dataset.permissionMode + '"]',
      );
      box.hidden = select.checked;
    }
    const roomRows = [
      ...form.querySelectorAll('[data-permission-section="room"] [data-permission-row]'),
    ];
    for (const controls of form.querySelectorAll('[data-inherit-lifetime="true"]')) {
      if (controls.dataset.lifetimeEdited) continue;
      const row = controls.closest('[data-permission-row]');
      const parent = roomRows.find(
        (item) =>
          item.dataset.projectId === row.dataset.projectId &&
          item.dataset.environmentId === row.dataset.environmentId,
      );
      if (!parent) continue;
      const lifetime = permissionLifetimeValue(parent);
      controls.dataset.originalLifetime = JSON.stringify(lifetime);
      controls.querySelector('[data-permission-lifetime-mode]').value = lifetime.mode;
      controls.querySelector('[data-permission-lifetime-until]').value = permissionDateValue(
        lifetime.expires_at,
      );
    }
    // Room rows are evaluated first. Custom channels are bounded by this
    // unsaved room selection, not just the host's maximum grant.
    for (const group of form.querySelectorAll('[data-permission-row]')) {
      const channel = group.closest('[data-permission-section]').dataset.permissionSection;
      const parent =
        channel === 'room'
          ? null
          : roomRows.find(
              (item) =>
                item.dataset.projectId === group.dataset.projectId &&
                item.dataset.environmentId === group.dataset.environmentId,
            );
      const project = group.querySelector('[data-permission-project]');
      const parentEnabled = parent?.querySelector(
        '[data-permission-project]:checked:not(:disabled)',
      );
      project.disabled =
        !group.querySelector('[data-host-available="true"]') ||
        (channel !== 'room' && !parentEnabled);
      const enabled = project.checked && !project.disabled;
      const lifetimeMode = group.querySelector('[data-permission-lifetime-mode]');
      const lifetimeUntil = group.querySelector('[data-permission-lifetime-until]');
      lifetimeMode.disabled = !enabled;
      lifetimeUntil.disabled = !enabled || lifetimeMode.value !== 'timed';
      lifetimeUntil.closest('label').hidden = lifetimeMode.value !== 'timed';
      const lifetime = permissionLifetimeValue(group);
      group.querySelector('[data-permission-expired]').hidden =
        lifetime.mode !== 'timed' || !(Number(lifetime.expires_at) <= Date.now() / 1000);
      for (const input of group.querySelectorAll('[data-permission-cap]')) {
        const parentCap = parent?.querySelector(
          '[data-permission-cap][value="' + input.value + '"]:checked:not(:disabled)',
        );
        const available =
          input.dataset.hostAvailable === 'true' && (channel === 'room' || !!parentCap);
        input.dataset.available = String(available);
        input.disabled = !enabled || !available;
      }
      const execution = group.querySelector('[data-permission-cap][value="execute"]');
      const parentTargets = new Set(
        [
          ...(parent?.querySelectorAll('[data-permission-target-option]:checked:not(:disabled)') ||
            []),
        ].map((input) => input.value),
      );
      for (const input of group.querySelectorAll('[data-permission-target-option]')) {
        const available = channel === 'room' || parentTargets.has(input.value);
        input.dataset.available = String(available);
        input.disabled = !enabled || !execution?.checked || execution.disabled || !available;
      }
      const level = group.querySelector('[data-permission-level]');
      const allowedCaps = [
        ...group.querySelectorAll('[data-permission-cap][data-available="true"]'),
      ].map((input) => input.value);
      for (const option of level.options) {
        const caps = permissionLevels[option.value][1];
        option.hidden =
          option.value === 'run' && level.value !== 'run' && allowedCaps.includes('write');
        option.disabled =
          !caps.every((cap) => allowedCaps.includes(cap)) ||
          (caps.includes('execute') &&
            ![...group.querySelectorAll('[data-permission-target-option]')].some(
              (input) => input.dataset.available === 'true',
            ));
      }
      level.disabled = project.disabled;
      const targets = group.querySelector('[data-permission-targets]');
      if (targets) {
        targets.hidden = !enabled || !execution.checked || execution.disabled;
        const allowed = [...targets.querySelectorAll('input:not(:disabled)')];
        const selected = allowed.filter((input) => input.checked).length;
        targets.querySelector('summary').textContent =
          '运行位置：' + (selected === allowed.length ? '全部' : '已选') + '（' + selected + '）';
      }
      const expiry = group.querySelector('.cr-permission-expiry');
      expiry.hidden = !enabled;
      expiry.querySelector('summary').textContent = !group.querySelector(
        '[data-permission-expired]',
      ).hidden
        ? '有效期 · 已过期'
        : '有效期';
    }
    for (const section of form.querySelectorAll('[data-permission-section]')) {
      const projects = [...section.querySelectorAll('[data-permission-project]:not(:disabled)')];
      const selected = projects.filter((project) => project.checked).length;
      const all = section.querySelector('[data-permission-select-all]');
      all.disabled = !projects.length;
      all.checked = selected > 0 && selected === projects.length;
      all.indeterminate = selected > 0 && selected < projects.length;
      section.querySelector('[data-permission-count]').textContent =
        '已选 ' + selected + ' / ' + projects.length;
      for (const control of section.querySelectorAll(
        '[data-permission-bulk-level], [data-permission-global-expiry], [data-permission-global-date] input',
      ))
        control.disabled = !selected;
    }
    patchRoomPermissions();
  }
  function applyPermissionLevel(select, value, allTargets = false) {
    if (
      select.disabled ||
      ![...select.options].some((option) => option.value === value && !option.disabled)
    )
      return;
    const group = select.closest('[data-permission-row]');
    const caps = permissionLevels[value][1];
    group.querySelector('[data-permission-project]').checked = !!value;
    for (const input of group.querySelectorAll('[data-permission-cap]'))
      input.checked = caps.includes(input.value);
    if (
      caps.includes('execute') &&
      (allTargets ||
        !group.querySelector('[data-permission-target-option][data-available="true"]:checked'))
    )
      for (const input of group.querySelectorAll(
        '[data-permission-target-option][data-available="true"]',
      ))
        input.checked = true;
    select.value = value;
  }
  function applyGlobalPermissionExpiry(section) {
    const mode = section.querySelector('[data-permission-global-expiry]').value;
    const date = section.querySelector('[data-permission-global-date]');
    date.hidden = mode !== 'timed';
    if (!mode) return;
    for (const project of section.querySelectorAll(
      '[data-permission-project]:checked:not(:disabled)',
    )) {
      const controls = project
        .closest('[data-permission-row]')
        .querySelector('[data-permission-lifetime]');
      controls.dataset.lifetimeEdited = 'true';
      controls.querySelector('[data-permission-lifetime-mode]').value = mode;
      controls.querySelector('[data-permission-lifetime-until]').value =
        date.querySelector('input').value;
    }
  }
  function collectPermissionScope(section, parentScope = null) {
    const rules = [];
    for (const group of section.querySelectorAll('[data-permission-row]')) {
      if (!group.querySelector('[data-permission-project]:checked:not(:disabled)')) continue;
      const capabilities = [
        ...group.querySelectorAll('[data-permission-cap]:checked:not(:disabled)'),
      ].map((item) => item.value);
      if (!capabilities.length) throw new Error('所选项目至少需要一项可授予权限。');
      const project = {
        project_id: group.dataset.projectId,
        environment_id: group.dataset.environmentId,
      };
      const lifetime = permissionLifetimeValue(group);
      if (
        lifetime.mode === 'timed' &&
        (!Number.isFinite(lifetime.expires_at) || lifetime.expires_at <= Date.now() / 1000)
      )
        throw new Error('请更新“' + group.dataset.projectLabel + '”的有效期。');
      const parent = parentScope?.project_scopes?.find(
        (item) => permissionKey(item) === permissionKey(project),
      );
      if (
        parent?.lifetime?.mode === 'timed' &&
        (lifetime.mode !== 'timed' || lifetime.expires_at > parent.lifetime.expires_at)
      )
        throw new Error('渠道自定义有效期不能晚于对应房间项目的截止时间，请调整该行有效期。');
      const executionTargets = capabilities.includes('execute')
        ? [...group.querySelectorAll('[data-permission-target-option]:checked:not(:disabled)')].map(
            (input) => input.value,
          )
        : [];
      if (capabilities.includes('execute') && !executionTargets.length)
        throw new Error('允许执行的项目需要选择至少一个执行位置。');
      rules.push({
        ...project,
        capabilities,
        execution_targets: executionTargets,
        lifetime,
      });
    }
    return {
      project_ids: [...new Set(rules.map((item) => item.project_id))],
      project_scopes: rules,
    };
  }
  function restrictHiddenChannel(prior, parent) {
    const rules = (prior?.project_scopes || []).flatMap((rule) => {
      const ceiling = parent.project_scopes.find(
        (item) => permissionKey(item) === permissionKey(rule),
      );
      if (!ceiling) return [];
      const capabilities = rule.capabilities.filter((cap) => ceiling.capabilities.includes(cap));
      if (!capabilities.includes('read')) return [];
      const execution_targets = capabilities.includes('execute')
        ? rule.execution_targets.filter((target) => ceiling.execution_targets.includes(target))
        : [];
      const deadlines = [rule.lifetime.expires_at, ceiling.lifetime.expires_at].filter(
        Number.isFinite,
      );
      const expires_at = deadlines.length ? Math.min(...deadlines) : null;
      if (expires_at !== null && expires_at <= Date.now() / 1000) return [];
      return [
        {
          ...rule,
          capabilities: capabilities.filter((cap) => cap !== 'execute' || execution_targets.length),
          execution_targets,
          lifetime: { mode: expires_at === null ? 'until_revoked' : 'timed', expires_at },
        },
      ];
    });
    return {
      project_ids: [...new Set(rules.map((rule) => rule.project_id))],
      project_scopes: rules,
    };
  }
  function continuationReason(code) {
    return (
      {
        CHANNEL_OWNER_NOT_APPROVED: '需要先在微信连接里重新允许本人交办',
        ROOM_SOURCE_CHANGED: '原任务消息已修改或撤回',
        ROOM_OWNER_SOURCE_REQUIRED: '无法核实原任务的房主身份',
        ROOM_CONTINUATION_SCOPE_DENIED: '当前权限未覆盖原任务的剩余操作',
        ROOM_CONTINUATION_MANUALLY_PAUSED: '房主已暂停这项工作',
        ROOM_CONTINUATION_ALREADY_STARTED: '这条消息已经开工',
        ROOM_CONTINUATION_POLICY_CHANGED: '批准后房间权限已变化',
        ROOM_CONTINUATION_UNAVAILABLE: '原工作已结束或无法续办',
        ROOM_CONTINUATION_EVIDENCE_CHANGED: '原续办凭证已变化',
        ROOM_RECOVERY_MEMBER_UNAVAILABLE: '当前没有可接手原任务的 dot',
        ROOM_INTENT_CHANGED: '原工作的要求已变化',
      }[code] || reasonText(code)
    );
  }
  function continuationSummary(result) {
    const counts = result.counts || {};
    return `续办：${counts.continued || 0} 项已继续，${counts.waiting_operations || 0} 项待核对，${counts.skipped || 0} 项跳过${counts.pending ? '，' + counts.pending + ' 项待处理' : ''}。`;
  }
  function renderContinuationResult(current) {
    if (panel !== current || current?.name !== 'permissions') return;
    const box = root.querySelector('[data-cr-continuation-result]');
    if (!box) return;
    const result = current.continuationResult;
    const states = {
      continued: '已继续',
      waiting_operations: '等待原操作回执',
      skipped: '已跳过',
      pending: '待处理',
    };
    const markup = result
      ? `<section class="cr-continuation" aria-label="续办结果"><h3>续办结果</h3><p role="status">${E(continuationSummary(result))}</p><ul>${(result.results || []).map((item) => `<li><strong>${E(item.label || current.continuationLabels?.get(item.task_id) || '原任务 #' + String(item.task_id).slice(0, 8))}</strong> · ${E(states[item.state] || '待核对')}${item.reason_code ? `<small>${E(continuationReason(item.reason_code))}</small>` : ''}</li>`).join('')}</ul></section>`
      : '';
    if (box._markup !== markup) {
      box.innerHTML = markup;
      box._markup = markup;
    }
  }
  async function refreshContinuationResult(current) {
    if (
      panel !== current ||
      current.name !== 'permissions' ||
      current.busy ||
      current.readingContinuation
    )
      return;
    const batchId = store.draft(current.roomId).permissionContinuation;
    if (
      !batchId ||
      (current.continuationResult?.batch_id === batchId &&
        current.continuationResult.counts?.pending === 0 &&
        current.continuationResult.counts?.waiting_operations === 0)
    )
      return;
    const epoch = state.epoch;
    const owner = state.owner;
    current.readingContinuation = true;
    try {
      const result = await store.permissionContinuation(current.roomId, batchId);
      if (
        panel !== current ||
        state.epoch !== epoch ||
        owner !== state.owner ||
        store.draft(current.roomId).permissionContinuation !== batchId
      )
        return;
      current.continuationResult = result;
      renderContinuationResult(current);
    } catch (error) {
      if (
        panel === current &&
        state.epoch === epoch &&
        owner === state.owner &&
        !current.continuationResult &&
        [403, 404].includes(error.status)
      )
        delete store.draft(current.roomId).permissionContinuation;
    } finally {
      current.readingContinuation = false;
    }
  }
  function updateContinuationSelection(form) {
    const tasks = [...form.querySelectorAll('[data-cr-continuation-task]:not(:disabled)')];
    const selected = tasks.filter((task) => task.checked).length;
    const all = form.querySelector('[data-cr-continuation-all]');
    if (all) {
      all.disabled = !tasks.length;
      all.checked = selected > 0 && selected === tasks.length;
      all.indeterminate = selected > 0 && selected < tasks.length;
    }
    const count = form.querySelector('[data-cr-continuation-count]');
    if (count) count.textContent = '已选 ' + selected + ' / ' + tasks.length;
    form.querySelector('button[type="submit"]').textContent = selected ? '保存并继续' : '保存权限';
  }
  function continuationPreviewMarkup(preview) {
    const candidates = preview.candidates || [];
    if (!candidates.length) return '<p class="cr-muted">当前权限下没有可继续的任务。</p>';
    return `<div class="cr-continuation-select"><label class="cr-choice"><input type="checkbox" data-cr-continuation-all><span>全选</span></label><span class="cr-muted" data-cr-continuation-count aria-live="polite"></span></div><div class="cr-continuation-tasks">${candidates
      .map((item) => {
        const eligible = item.eligible !== false;
        const remaining = (item.remaining_operations || []).map((op) => op.label).filter(Boolean);
        const detail = eligible
          ? remaining.join('、') || '继续处理原请求'
          : continuationReason(item.reason_code) || '当前无法续办';
        return `<label class="cr-choice"><input type="checkbox" data-cr-continuation-task value="${E(item.task_id)}" ${!eligible ? 'disabled' : item.selected_by_default !== false ? 'checked' : ''}><span><strong>${E(item.label || '未完成的任务')}</strong><small>${E(detail)}</small></span></label>`;
      })
      .join('')}</div>`;
  }
  async function previewContinuation(form, current) {
    current.continuationRequest?.abort();
    current.continuationPreview = null;
    const enabled = form.querySelector('[data-cr-continue-selected]').checked;
    const box = form.querySelector('[data-cr-continuation-preview]');
    const submit = form.querySelector('button[type="submit"]');
    const owner = state.owner;
    const epoch = state.epoch;
    const selection = new Map(
      [...box.querySelectorAll('[data-cr-continuation-task]:not(:disabled)')].map((task) => [
        task.value,
        task.checked,
      ]),
    );
    if (!enabled) current.continuationSelection = null;
    else if (selection.size) current.continuationSelection = selection;
    box.hidden = !enabled;
    box.replaceChildren();
    updateContinuationSelection(form);
    submit.disabled = enabled || !activeRoom() || current.busy;
    if (!enabled) return;
    const request = new AbortController();
    current.continuationRequest = request;
    const visible = () =>
      panel === current &&
      form.isConnected &&
      owner === state.owner &&
      epoch === state.epoch &&
      !request.signal.aborted;
    box.innerHTML = '<p class="cr-muted" role="status">正在核对可继续的任务…</p>';
    try {
      const draft = permissionDraft(form, current);
      const preview = await store.previewPermissionContinuation(
        current.roomId,
        draft,
        request.signal,
      );
      if (!visible()) return;
      current.continuationPreview = { ...preview, draft: JSON.stringify(draft) };
      box.innerHTML = continuationPreviewMarkup(preview);
      for (const task of box.querySelectorAll('[data-cr-continuation-task]:not(:disabled)'))
        if (current.continuationSelection?.has(task.value))
          task.checked = current.continuationSelection.get(task.value);
      updateContinuationSelection(form);
      submit.disabled = !activeRoom() || current.busy;
    } catch (error) {
      if (!visible()) return;
      box.innerHTML = `<p role="status">${E(error.message || '暂时无法核对续办清单。')}</p>${btn('refresh-continuation-preview', '重新核对', '', 'cr-text-button')}`;
    }
    patchRoomPermissions();
  }
  function permissionDraft(form, current) {
    const original = current.permissions;
    const scope = collectPermissionScope(form.querySelector('[data-permission-section="room"]'));
    const channels = {};
    for (const channel of ['weixin', 'telegram']) {
      const control = form.querySelector('[data-permission-mode="' + channel + '"]');
      const prior = original.channels?.[channel];
      const mode = control
        ? control.checked
          ? 'follow_room'
          : 'custom'
        : prior?.mode || 'follow_room';
      channels[channel] = {
        mode,
        scope:
          mode === 'custom'
            ? control
              ? collectPermissionScope(
                  form.querySelector('[data-permission-section="' + channel + '"]'),
                  scope,
                )
              : restrictHiddenChannel(prior?.scope, scope)
            : { project_scopes: [] },
      };
    }
    return { expected_version: original.version, scope, channels };
  }
  function pausedProjectNotice(result) {
    const blocked = result?.blocked_projects || [];
    if (!blocked.length) return '';
    const labels = blocked.map((item) => {
      const project = (result.available_projects || []).find(
        (entry) =>
          entry.project_id === item.project_id && entry.environment_id === item.environment_id,
      );
      const name =
        project?.name ||
        (S.projects || []).find((entry) => entry.id === item.project_id)?.alias ||
        '项目';
      return item.environment_id === 'production' ? name : name + '（' + item.environment_id + '）';
    });
    return '已跳过暂停项目 ' + labels.join('、') + ' 的新增授权。';
  }
  async function savePermissions(form, current) {
    if (current.stale) throw new Error('权限已在其他位置更新，请重新载入后核对。');
    updatePermissionControls();
    const owner = state.owner;
    let confirmed = null;
    let confirmedContinuation = null;
    let refreshPreview = false;
    const draft = permissionDraft(form, current);
    const preview = current.continuationPreview;
    const enabled = form.querySelector('[data-cr-continue-selected]').checked;
    const selectedTaskIds = [
      ...form.querySelectorAll('[data-cr-continuation-task]:checked:not(:disabled)'),
    ].map((task) => task.value);
    if (
      enabled &&
      (!preview ||
        preview.draft !== JSON.stringify(draft) ||
        (preview.expires_at && preview.expires_at <= Date.now() / 1000))
    ) {
      void previewContinuation(form, current);
      throw new Error('权限或续办清单已变化，请核对后保存。');
    }
    const continuation =
      enabled && selectedTaskIds.length
        ? {
            policy_preview_digest: preview.policy_preview_digest,
            continuation_preview_id: preview.continuation_preview_id,
            continue_selected: true,
            selected_task_ids: selectedTaskIds,
          }
        : {};
    const controls = [...form.querySelectorAll('input,select,button')].map((control) => [
      control,
      control.disabled,
    ]);
    for (const [control] of controls) control.disabled = true;
    let result;
    try {
      result = await mutation(
        'PUT',
        store.path(current.roomId) + '/permissions',
        { ...draft, ...continuation },
        current,
        {
          onConfirmed: (response) => {
            confirmed = response;
            if (response.continuation?.batch_id) {
              confirmedContinuation = response.continuation;
              current.continuationResult = confirmedContinuation;
              current.continuationLabels = new Map(
                (preview?.candidates || []).map((item) => [item.task_id, item.label]),
              );
              store.draft(current.roomId).permissionContinuation = response.continuation.batch_id;
            }
          },
        },
      );
    } catch (error) {
      refreshPreview = panel === current && retryableContinuationPreview.has(error.code);
      throw error;
    } finally {
      for (const [control, disabled] of controls)
        if (control.isConnected) control.disabled = disabled;
      if (refreshPreview) void previewContinuation(form, current);
      patchRoomPermissions();
    }
    const pausedNotice = pausedProjectNotice(result || confirmed);
    const savedMessage =
      (pausedNotice ? '已保存可应用的权限。' + pausedNotice : '权限设置已保存。') +
      (confirmedContinuation ? continuationSummary(confirmedContinuation) : '');
    const invalidation = current.accessInvalidation;
    const receiptOnly =
      confirmed &&
      !result &&
      !panel &&
      owner === state.owner &&
      current.roomId === state.roomId &&
      invalidation?.owner === owner &&
      invalidation.roomId === state.roomId &&
      invalidation.epoch === state.epoch &&
      invalidation.openingEpoch === root?.querySelector('dialog')?._crOpeningEpoch;
    if (receiptOnly) {
      const feedback = {
        owner,
        roomId: current.roomId,
        epoch: state.epoch,
        openingEpoch: current.epoch,
        message: savedMessage,
      };
      root._crProjectSaveFeedback = feedback;
      announce(feedback.message);
      try {
        await store.refresh();
        if (root?._crProjectSaveFeedback === feedback) delete root._crProjectSaveFeedback;
      } catch (error) {
        if (
          error.name !== 'AbortError' &&
          owner === state.owner &&
          current.roomId === state.roomId &&
          feedback.epoch === state.epoch &&
          !panel &&
          feedback.openingEpoch === root?.querySelector('dialog')?._crOpeningEpoch
        )
          announce(feedback.message + ' 房间视图刷新失败：' + error.message, true);
      }
      return;
    }
    if (!result || panel !== current) return;
    renderPermissions(result);
    panelError(savedMessage);
    const feedback = {
      owner,
      roomId: current.roomId,
      epoch: state.epoch,
      openingEpoch: current.epoch,
      message: savedMessage,
    };
    current.savedPermissionFeedback = feedback;
    root._crProjectSaveFeedback = feedback;
    announce(feedback.message);
    const savedPanel =
      current.returnChannel && !confirmedContinuation && !pausedNotice
        ? await returnToChannel(current)
        : current;
    try {
      await store.refresh();
      if (root?._crProjectSaveFeedback === feedback) delete root._crProjectSaveFeedback;
    } catch (error) {
      if (error.name === 'AbortError') return;
      const warning = feedback.message + ' 房间视图刷新失败：' + error.message;
      if (panel === savedPanel) panelError(warning);
      else if (
        feedback.accessInvalidated &&
        !panel &&
        owner === state.owner &&
        current.roomId === state.roomId &&
        feedback.epoch === state.epoch &&
        feedback.openingEpoch === root?.querySelector('dialog')?._crOpeningEpoch
      )
        announce(warning, true);
    }
  }
  function membersPanel() {
    if (!state.room) return;
    openPanel(
      'members',
      '房间成员',
      `<div class="cr-member-list">${
        members().length
          ? members()
              .map(
                (member) =>
                  `<article class="cr-member"><span class="cr-member-avatar" aria-hidden="true">${E((member.name || member.label || 'D').slice(0, 1))}</span><div><h3>${E(member.name || member.label)}${state.room.lead_member_id === memberId(member) ? '<span class="cr-tag">主责</span>' : ''}</h3><p>${E(memberBadges[member.stage || member.state] || '连接中')}</p><small>${E(memberOrigin(member))}</small></div>${btn('member-detail', '管理', `data-id="${E(memberId(member))}" aria-label="管理 ${E(member.name || member.label)} ${E(memberShort(member))}"`)}</article>`,
              )
              .join('')
          : '<p class="cr-muted">连接一个 dot，让工作持续推进。</p>'
      }</div>${btn('add-member', '添加 dot', '', 'cr-primary cr-full')}`,
    );
  }
  async function addMemberPanel() {
    if (!activeRoom()) {
      announce(readOnlyReason(), true);
      return;
    }
    const roomId = state.roomId,
      epoch = state.epoch,
      opening = panelEpoch;
    const policy = await api(store.path(roomId) + '/permissions');
    if (roomId !== state.roomId || epoch !== state.epoch || opening !== panelEpoch) return;
    if (!policy.configured) {
      const initialized = await mutation('PUT', store.path(roomId) + '/permissions', {
        expected_version: policy.version,
        scope: policy.scope,
        channels: Object.fromEntries(
          Object.entries(policy.channels || {}).map(([name, entry]) => [
            name,
            { mode: entry.mode, scope: entry.scope },
          ]),
        ),
      });
      if (
        !initialized ||
        roomId !== state.roomId ||
        epoch !== state.epoch ||
        opening !== panelEpoch
      )
        return;
      await store.refresh();
      if (roomId !== state.roomId || epoch !== state.epoch || opening !== panelEpoch) return;
    }
    openPanel(
      'add-member',
      '添加 dot',
      `<form data-cr-form="member"><fieldset class="cr-connection-mode"><legend class="sr-only">选择连接方式</legend><label><input type="radio" name="connection_mode" value="new" checked><span>新 dot</span></label><label><input type="radio" name="connection_mode" value="existing"><span>已有 dot</span></label></fieldset><section data-cr-connections hidden>${field('已验证的连接', '<select name="reuse_member_id" disabled><option value="">选择已验证的 dot</option></select>')}<p class="cr-muted" data-cr-connection-status></p>${btn('more-connections', '读取已有连接', 'data-cr-more-connections', 'cr-text-button')}<p class="cr-info">加入后使用房间的项目权限。</p></section>${field('成员名称', '<input name="name" required maxlength="80" placeholder="给这个 dot 起个名字" autofocus>')}<section class="cr-connect-summary" aria-label="接入范围"><p class="cr-muted">dot 加入「${E(roomName(state.room))}」并使用房间已批准的项目权限。读取和回复探测通过后就可在房间收发消息。</p><label class="cr-choice"><input type="checkbox" name="keep_connected"><span>在房间授权有效期间保持连接<small>由 CodePier 定期续期。可在连接详情关闭。</small></span></label></section><button class="cr-button cr-primary" type="submit">添加并连接</button></form>`,
      {
        version: state.room.version,
        connections: [],
        connectionsCursor: '',
        connectionsLoaded: false,
      },
    );
    void loadConnections();
  }
  async function loadConnections(more = false) {
    if (panel?.name !== 'add-member' || panel.connectionsLoading) return;
    const current = panel;
    current.connectionsLoading = true;
    const status = root.querySelector('[data-cr-connection-status]');
    status.textContent = '正在加载已连接的 dot…';
    const moreButton = root.querySelector('[data-cr-more-connections]');
    moreButton.disabled = true;
    try {
      const response = await api(
        base +
          '/connections?limit=40' +
          (more && current.connectionsCursor
            ? '&cursor=' + encodeURIComponent(current.connectionsCursor)
            : ''),
      );
      if (current !== panel) return;
      current.connections = [
        ...new Map(
          [...(more ? current.connections : []), ...(response.items || [])]
            .filter((item) => item.conversation_id !== current.roomId)
            .map((item) => [item.member_id || item.id, item]),
        ).values(),
      ];
      current.connectionsCursor = response.next_cursor || '';
      current.connectionsLoaded = true;
      const select = root.querySelector('[name="reuse_member_id"]'),
        selected = select.value;
      select.innerHTML =
        '<option value="">选择已验证的 dot</option>' +
        current.connections
          .map(
            (item) =>
              `<option value="${E(item.member_id || item.id)}">${E(item.name)} · ${E(item.room_title || '原房间')}</option>`,
          )
          .join('');
      select.value = selected;
      const modes = [...root.querySelectorAll('[name="connection_mode"]')];
      if (!more && current.connections.length && !current.modeTouched) {
        const existing = modes.find((input) => input.value === 'existing');
        existing.checked = true;
        existing.dispatchEvent(new Event('change', { bubbles: true }));
      }
      status.textContent = current.connections.length
        ? ''
        : current.connectionsCursor
          ? '当前页没有可复用的连接，可以继续查看。'
          : '其他房间还没有连好的 dot。';
      moreButton.hidden = !current.connectionsCursor;
      moreButton.textContent = '更多已验证连接';
    } catch (error) {
      if (current === panel) {
        status.textContent = error.message;
        moreButton.hidden = false;
        moreButton.textContent = '重新读取';
      }
    } finally {
      current.connectionsLoading = false;
      if (current === panel) moreButton.disabled = false;
    }
  }
  async function returnToChannel(current) {
    if (panel !== current || current.roomId !== state.roomId || !current.returnChannel) return;
    openPanel('weixin', '连接微信', '<div data-cr-channel-view></div>');
    const destination = panel;
    renderChannel(current.returnChannel);
    try {
      await refreshChannel();
    } catch (error) {
      if (panel === destination) panelError('微信状态刷新失败：' + error.message);
    }
    return destination;
  }
  async function memberPanel(id) {
    const member = members().find((m) => memberId(m) === id);
    if (!member) return;
    openPanel(
      'member',
      member.name || member.label,
      `<div data-cr-flow-view><p class="cr-muted">正在读取连接状态…</p></div>`,
      { member, flowId: member.flow_id },
    );
    if (member.flow_id) await refreshFlow();
    else
      renderFlow({
        member,
        flow: { stage: member.state, version: member.version, display: member.display },
      });
  }
  function instructions(value) {
    if (!value) return '';
    return typeof value === 'string' ? value : value.text || value.prompt || '';
  }
  function connectionRenewalMarkup(renewal, member, pendingBinding) {
    if (!renewal) return '';
    const managed = renewal.approved === true && renewal.mode === 'managed';
    const end = renewal.approved_until || renewal.expires_at;
    const mode = managed ? 'CodePier 定期保持连接' : '由 dot 宿主负责续期';
    const status = {
      active: managed ? '续期已启用' : '',
      stopped: '续期已停止',
      awaiting_probe: '等待连接验证',
      waiting: '等待 dot 恢复',
      expired: '连接租期已到',
    }[renewal.state];
    const control =
      activeRoom() && member && !member.legacy && member.state !== 'removed' && !pendingBinding
        ? btn(
            'connection-renewal',
            managed ? '关闭托管续期' : '开启托管续期',
            `data-renewal-enable="${!managed}"`,
            'cr-text-button',
          )
        : '';
    return `<section class="cr-connect-summary" aria-label="连接保持方式"><strong>${E(mode)}</strong>${status ? `<p class="cr-muted">${E(status)}</p>` : ''}${end ? `<p class="cr-muted">${managed ? '批准至' : '当前租期至'} ${E(dateTime(end))}</p>` : ''}${control}</section>`;
  }
  function renderFlow(result) {
    if (!panel || !root) return;
    const flow = result.flow || result,
      member = result.member || panel.member;
    if (panel.flow && Number(panel.flow.version || 0) > Number(flow.version || 0)) return;
    panel.flow = flow;
    panel.flowId ||= flow.id;
    panel.member = member;
    panel.joinInstructions = instructions(
      result.join_instructions || result.resume_instructions || result.continue_instructions,
    );
    const box = root.querySelector('[data-cr-flow-view]');
    if (!box) return;
    const primary =
      flow.display?.primary_action || flow.primary_action || member?.display?.primary_action;
    const stage = flow.stage || flow.state || member?.state;
    const ready = ['ready_chat', 'ready_work'].includes(stage);
    if (ready) panel.joinInstructions = '';
    const renewal = flow.subscription_renewal || member?.subscription_renewal;
    const renewalMarkup = connectionRenewalMarkup(renewal, member, flow.pending_binding);
    const markup = `<div class="cr-flow-state" data-stage="${E(stage)}"><span class="cr-flow-symbol ${ready ? 'is-ready' : ''}" aria-hidden="true">${ready ? '✓' : icon('network')}</span><h3>${E(flow.display?.title || title(flow))}</h3>${flow.display?.reason ? `<p>${E(flow.display.reason)}</p>` : ''}</div>${renewalMarkup}${flow.pending_binding ? '<p class="cr-info">新连接验证通过后才会替换当前连接。原成员和工作保持原来的身份。</p>' : member?.connection_source || member?.connection_source_member_id ? '<p class="cr-info">请在已选择的原 dot 中完成本房间的独立接入与验证。此房间的参与范围单独生效。</p>' : ''}${panel.joinInstructions ? `<label class="cr-field"><span>加入说明</span><textarea class="cr-instructions" readonly rows="7" aria-label="dot 加入说明"></textarea></label>${btn('copy-instructions', '复制加入说明', '', 'cr-primary cr-full')}` : ''}${primary && !['copy_instructions', 'copy_join_instructions', 'send_message', 'start_work', 'none'].includes(primary) ? btn('flow-action', actionLabel(primary), `data-flow-action="${E(primary)}"`, 'cr-primary cr-full') : ''}${member ? `<section class="cr-member-controls">${btn('permissions', '权限设置')}${member.legacy ? btn('flow-action', '迁移此连接', 'data-flow-action="migrate"') : member.state !== 'removed' ? btn('rebind-member', flow.pending_binding ? '查看更换说明' : '更换连接') : ''}${state.room?.lead_member_id !== memberId(member) && member.state !== 'removed' ? btn('lead', '设为主责', `data-id="${E(memberId(member))}"`) : ''}${member.state !== 'removed' && member.state !== 'paused' ? btn('flow-action', '暂停', 'data-flow-action="pause"') : ''}${member.state !== 'removed' ? btn('remove-member', '移除成员', '', 'cr-danger') : ''}</section>` : ''}<details class="cr-diagnostics"><summary>连接详情</summary><dl>${Object.entries(
      flow.evidence || {},
    )
      .map(
        ([key, value]) =>
          `<div><dt>${E({ member_created: '成员已创建', host_bound: '宿主已登记', subscription_verified: '通知通路已验证', round_trip_verified: '消息往返已验证' }[key] || key)}</dt><dd>${value === true ? '已完成' : value === false ? '尚未完成' : E(value)}</dd></div>`,
      )
      .join('')}</dl><p>${E(flow.last_error_code || '')}</p></details>`;
    if (box._markup !== markup) {
      box.innerHTML = markup;
      box._markup = markup;
    }
    const area = box.querySelector('.cr-instructions');
    if (area) area.value = panel.joinInstructions;
    patchRoomPermissions();
  }
  async function refreshFlow() {
    if (!panel?.flowId || panel.busy) return;
    const current = panel,
      epoch = panelEpoch,
      writeEpoch = current.writeEpoch || 0;
    const response = await api(base + '/connect-flows/' + encodeURIComponent(current.flowId));
    if (panel === current && epoch === panelEpoch && writeEpoch === (current.writeEpoch || 0))
      renderFlow(response);
  }
  async function mutation(method, url, body, current = panel, options = {}) {
    requireMutation(method, url, body, current?.roomId || state.roomId);
    // Background polls (QR status) are read-like and keyed per flow version.
    // They never hijack, and are never hijacked by, an explicit user action.
    const unresolved = options.transient
      ? null
      : [...state.mutations.values()].find(
          (item) => item.state !== 'not_saved' && item.method === method && item.url === url,
        );
    if (unresolved) {
      await recoverAction(unresolved.id);
      return null;
    }
    const epoch = state.epoch,
      owner = state.owner,
      opening = panelEpoch;
    const requestBody = JSON.stringify({
      ...body,
      idempotency_key: body.idempotency_key || current?.key || store.key(),
    });
    const kind =
      url === base + '/rooms'
        ? 'create'
        : url.endsWith('/migration')
          ? 'migration'
          : url.includes('/legacy-channels/')
            ? 'legacy-channel'
            : url.endsWith('/members')
              ? 'member'
              : url.includes('/connect-flows/')
                ? 'flow'
                : url.includes('/channel-connect-flows/') ||
                    url.endsWith('/channels/weixin/connect')
                  ? 'channel'
                  : url.includes('/work/')
                    ? 'work'
                    : 'room';
    const receipt = {
      id: JSON.parse(requestBody).idempotency_key,
      method,
      url,
      body: requestBody,
      owner,
      roomId: current?.roomId || state.roomId,
      kind,
      label: {
        create: '创建房间',
        member: '添加成员',
        flow: '成员连接调整',
        channel: '微信连接调整',
        work: '工作控制',
        room: '房间设置调整',
        migration: '启用新房间界面',
        'legacy-channel': '暂停原渠道连接',
      }[kind],
      state: 'unknown',
    };
    if (current) {
      current.busy = true;
      current.writeEpoch = (current.writeEpoch || 0) + 1;
      if (!options.transient) panelError('');
    }
    const forms = options.transient ? [] : [...(root?.querySelectorAll('dialog form') || [])];
    for (const form of forms) {
      form.setAttribute('aria-busy', 'true');
      for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = true;
    }
    const actionButtons = options.transient
      ? []
      : [...(root?.querySelectorAll('.cr-panel-content button[data-cr-action]') || [])];
    for (const button of actionButtons) button.disabled = true;
    try {
      const response = await api(url, {
        method,
        body: requestBody,
      });
      if (owner !== state.owner) return null;
      options.onConfirmed?.(response);
      if (epoch === state.epoch && response.room) {
        store.invalidateReads();
        store.applyRoom(response);
      }
      if (current && current !== panel) return null;
      if (current) current.key = store.key();
      return response;
    } catch (error) {
      if (owner !== state.owner) throw error;
      if (!options.transient && (error.code === 'NETWORK_UNCERTAIN' || error.status >= 500)) {
        state.mutations.set(receipt.id, receipt);
        patchPending();
      }
      if (options.transient) throw error;
      // A late rejection belongs to its original room and panel opening.
      // Keep uncertain receipts above, but never overwrite a newer view.
      if (epoch !== state.epoch || receipt.roomId !== state.roomId || opening !== panelEpoch)
        throw error;
      if (current === panel) {
        panelError(error.message);
        if (current?.name === 'permissions') {
          // A rejected permission write may be corrected, but an uncertain
          // result or reused identity must retain its original recovery key.
          if (
            error.status >= 400 &&
            error.status < 500 &&
            error.status !== 408 &&
            !['NETWORK_UNCERTAIN', 'IDEMPOTENCY_CONFLICT', 'IDEMPOTENCY_RESPONSE_EXPIRED'].includes(
              error.code,
            )
          )
            current.key = store.key();
          if (error.code === 'STALE_VERSION') {
            current.stale = true;
            panelError('权限已在其他位置更新。你的选择尚未保存，请重新载入后核对。');
          }
          // Other 409s are business errors, not proof of a newer policy.
          throw error;
        }
        if (error.status === 409) {
          await store.refresh().catch(() => {});
          if (current !== panel || owner !== state.owner || epoch !== state.epoch) throw error;
          panelError('房间刚有更新。你的选择已保留，请核对后再次保存。');
          if (panel && state.room) {
            panel.version = state.room.version;
            panel.key = panel.name === 'migration' ? '' : store.key();
            if (panel.name === 'work') {
              const reviewing = panel;
              await store.work(reviewing.work.id).catch(() => {});
              if (panel === reviewing) {
                const latest = state.works.find((work) => work.id === reviewing.work.id);
                if (latest || state.workDetail?.id === reviewing.work.id)
                  renderWork(latest || state.workDetail);
                panelError('原工作刚有更新，请核对当前连接和原操作后再继续。');
              }
            }
            if (panel.name === 'migration') {
              panel.migrationRevision = null;
              const box = root.querySelector('[data-cr-migration-view]');
              if (box) delete box._markup;
              renderMigration();
              panelError('房间或迁移条件已变化，请重新核对并确认。');
            }
          }
        }
      } else announce(error.message, true);
      throw error;
    } finally {
      if (current) current.busy = false;
      for (const form of forms) {
        form.removeAttribute('aria-busy');
        for (const button of form.querySelectorAll('button[type="submit"]'))
          button.disabled = false;
      }
      for (const button of actionButtons) if (button.isConnected) button.disabled = false;
      patchRoomPermissions();
    }
  }
  async function recoverAction(id) {
    const receipt = state.mutations.get(id);
    if (
      !receipt ||
      ['recovering', 'not_saved'].includes(receipt.state) ||
      receipt.owner !== state.owner
    )
      return;
    // Exact original receipts remain queryable after a room becomes read-only.
    // Hub returns an existing receipt, or rejects unsaved work using the live
    // read-only/CAS boundary. Never refresh a version or mint a replacement key.
    if (JSON.parse(receipt.body).idempotency_key !== receipt.id)
      throw new Error('原请求身份无法核对，请保留当前记录。');
    const epoch = state.epoch;
    receipt.state = 'recovering';
    patchPending();
    let saved = false;
    try {
      // The exact serialized request survives dialog closure. A new form or
      // newer scope projection must never silently mint a replacement write.
      const result = await api(receipt.url, { method: receipt.method, body: receipt.body });
      if (receipt.owner !== state.owner) return;
      saved = true;
      state.mutations.delete(id);
      patchPending();
      if (result.continuation?.batch_id)
        store.draft(receipt.roomId).permissionContinuation = result.continuation.batch_id;
      if (receipt.kind === 'create') await store.list();
      if (!root || epoch !== state.epoch) return;
      store.invalidateReads();
      if (result.room) store.applyRoom(result);
      if (receipt.kind === 'create') {
        closePanel();
        requestedProject = '';
        await selectRoom(result.room.id);
      } else if (receipt.roomId === state.roomId) {
        if (receipt.kind === 'member' || receipt.kind === 'flow') {
          openPanel('member', result.member?.name || 'dot 连接', '<div data-cr-flow-view></div>', {
            member: result.member,
            flowId: result.flow?.id,
          });
          renderFlow(result);
        } else if (receipt.kind === 'channel') {
          openPanel('weixin', '连接微信', '<div data-cr-channel-view></div>');
          renderChannel(result);
        } else if (receipt.kind === 'work') {
          store.applyWork(result.work);
          workPanel(result.work.id);
        } else closePanel();
        await store.refresh();
      }
      announce('已核对并恢复原操作');
    } catch (error) {
      if (receipt.owner === state.owner) {
        if (!saved && receipt.kind === 'migration' && error.not_saved === true) {
          receipt.state = 'not_saved';
          receipt.error = '服务器已确认原启用请求未保存。请重新核对当前迁移条件，再明确确认。';
          patchPending();
          if (root && epoch === state.epoch && receipt.roomId === state.roomId) {
            if (panel?.name === 'migration') {
              panel.key = '';
              panel.migrationRevision = null;
              const box = root.querySelector('[data-cr-migration-view]');
              if (box) delete box._markup;
              renderMigration();
            }
            await store.refresh().catch(() => {});
            if (epoch === state.epoch)
              panel?.name === 'migration' ? panelError(receipt.error) : announce(receipt.error);
          }
          return;
        }
        if (!saved && receipt.kind === 'channel' && error.status === 409) {
          // A WeChat step is checked against its flow version before any
          // write, so a conflict on the exact original key proves it was not
          // saved. Drop it instead of replaying it on every later click.
          state.mutations.delete(id);
          patchPending();
          if (root && epoch === state.epoch && receipt.roomId === state.roomId) {
            if (panel?.name === 'weixin') {
              panelError('原请求未保存（连接状态已前进），已按当前状态刷新。');
              await refreshChannel().catch(() => {});
            } else announce('原微信连接请求未保存，状态已前进。');
          }
          return;
        }
        if (!saved) receipt.state = 'unknown';
        const message =
          (saved ? '原操作已保存，视图需要重新读取：' : '原操作尚未确认：') + error.message;
        if (!saved) receipt.error = message;
        panel ? panelError(message) : announce(message, true);
        patchPending();
      }
      throw error;
    }
  }
  async function submitForm(form) {
    const current = panel;
    if (!current || current.busy) return;
    const data = new FormData(form),
      type = form.dataset.crForm;
    if (type === 'create') {
      const result = await mutation('POST', base + '/rooms', {
        title: data.get('title').trim(),
        projects: data
          .getAll('project')
          .map((project) => ({ project, environment_id: 'production' })),
      });
      if (!result) return;
      const room = result.room || result;
      await store.list();
      closePanel();
      requestedProject = '';
      await selectRoom(room.id);
      const pausedNotice = pausedProjectNotice(result);
      if (pausedNotice) announce('房间已创建。' + pausedNotice);
    } else if (type === 'migration') {
      if (current.migrationRevision !== migrationRevision()) {
        renderMigration();
        panelError('房间或迁移条件已变化，请重新核对并确认。');
        return;
      }
      if (!state.room?.legacy || state.room?.migration?.eligible !== true) {
        panelError('请先处理服务器列出的迁移条件，再明确确认启用。');
        return;
      }
      const revision = current.migrationRevision;
      const expectedVersion = current.version;
      const confirmed = await uiConfirmAction(
        {
          title: '启用新版房间？',
          body: '沿用当前已批准的范围。',
          confirmLabel: '启用',
        },
        {
          owner: form,
          current: () => panel === current && revision === migrationRevision(),
        },
      );
      if (!confirmed) return;
      const result = await mutation('POST', store.path() + '/migration', {
        action: 'activate',
        expected_version: expectedVersion,
      });
      if (result) {
        closePanel();
        await store.refresh();
        announce('已切换到新版房间。');
      }
    } else if (type === 'permissions') {
      await savePermissions(form, current);
    } else if (type === 'member') {
      const result = await mutation('POST', store.path() + '/members', {
        name: data.get('name').trim(),
        expected_version: current.version,
        ...(data.get('keep_connected') ? { keep_connected: true } : {}),
        ...(data.get('connection_mode') === 'existing'
          ? {
              reuse_member_id: data.get('reuse_member_id'),
              reuse_binding_generation: current.connections.find(
                (item) => (item.member_id || item.id) === data.get('reuse_member_id'),
              )?.binding_generation,
            }
          : {}),
      });
      if (!result) return;
      await store.refresh();
      if (panel !== current) return;
      openPanel(
        'member',
        result.member?.name || data.get('name'),
        '<div data-cr-flow-view></div>',
        { member: result.member, flowId: result.flow?.id || result.flow_id },
      );
      renderFlow(result);
    } else if (type === 'work-recovery') {
      const action = form.dataset.workAction;
      const candidates =
        current.work.recovery?.[
          action === 'takeover' ? 'takeover_candidates' : 'restore_candidates'
        ] || [];
      const selected = candidates.find(
        (member) => member.member_id === data.get('target_member_id'),
      );
      if (!selected) {
        panelError('请选择此工作当前允许的成员。');
        return;
      }
      await controlWork(action, selected.member_id);
    } else if (type === 'settings') {
      const result = await mutation('PATCH', store.path(), {
        title: data.get('title').trim(),
        expected_version: current.version,
      });
      if (result) {
        closePanel();
        await store.refresh();
      }
    } else if (type === 'weixin') await submitChannel(form);
  }
  async function selectRoom(id) {
    remember();
    rememberReading();
    closeHints();
    accessBlocked = false;
    closePanel();
    announce('');
    root?.classList.remove('cr-show-rooms');
    root?.querySelector('[data-cr-action="rooms"]')?.setAttribute('aria-expanded', 'false');
    const list = root?.querySelector('[data-cr-messages]');
    list?.replaceChildren();
    try {
      const promise = store.select(id);
      if (root) root._restoringReading = !!store.draft().reading;
      selection(id);
      const editor = root?.querySelector('#cr-message');
      if (editor) {
        editor.value = store.draft().text;
        sizeEditor(editor);
      }
      patchHeader();
      patchWorks();
      patchPending();
      await promise;
      if (state.roomId === id) {
        patchHeader();
        patchMessages();
        patchWorks();
        patchPending();
        await restoreReading();
        connectHints();
      }
    } catch (error) {
      if (error.name !== 'AbortError') announce(error.message, true);
    }
  }
  function diagnosticFields(fields) {
    return `<dl class="cr-facts">${fields.map(([label, value]) => `<div><dt>${E(label)}</dt><dd>${E(supplied(value))}</dd></div>`).join('')}</dl>`;
  }
  function renderDiagnostics(data) {
    const box = root?.querySelector('[data-cr-diagnostics-view]');
    if (!box || panel?.name !== 'diagnostics') return;
    const refresh = box.querySelector('[data-cr-action="refresh-diagnostics"]');
    const restoreRefresh = refresh && refresh === document.activeElement;
    const truncated = Object.values(data.truncated || {}).some(Boolean);
    const labels = {
      members: '成员',
      traces: '原记录',
      budgets: '额度',
      pending: '待处理输入',
      goals: '工作',
    };
    const traceLabels = {
      member: '成员',
      message: '原输入',
      goal: '工作',
      work: '步骤',
      operation: '原操作',
      delivery: '渠道投递',
    };
    const limitLabels = {
      members: '成员',
      goals: '工作',
      pending_per_member: '每成员待处理输入',
      traces: '原记录',
      budgets: '额度',
      messages: '原消息',
      parts_per_delivery: '每投递分项',
      dispatch_concurrency: '通知并发',
    };
    box.innerHTML = `<div class="cr-inspection-heading"><p class="cr-muted">只读快照 · ${E(dateTime(data.observed_at))}</p>${btn('refresh-diagnostics', '刷新')}</div><p class="cr-info">${data.read_only === true ? '查询不领取输入、不确认消费。' : '查询是否只读：未提供。'} ${data.redacted === true ? '仅显示已脱敏的当前授权记录。' : '脱敏状态：未提供。'} ${data.grants_authority === false ? '这些记录和额度不授予工作权限。' : '授权含义：未提供。'}</p><p class="cr-muted">有界观察窗口；待处理数量不是全局总数。未提供的值保持未知。</p>${
      truncated
        ? `<p class="cr-info" data-cr-diagnostics-incomplete>部分记录未载入：${E(
            Object.entries(data.truncated || {})
              .filter(([, value]) => value)
              .map(([name]) => labels[name] || name)
              .join('、'),
          )}。</p>`
        : ''
    }
      <section class="cr-inspection-section" data-cr-diagnostic-queues><h3>等待与队列</h3>${
        data.queues?.length
          ? data.queues
              .map(
                (queue) =>
                  `<article><h4>${E(queue.label || queue.kind)}</h4>${diagnosticFields([
                    ['窗口内待处理', queue.pending_count],
                    ['最早等待（秒）', queue.oldest_age_seconds],
                  ])}</article>`,
              )
              .join('')
          : '<p class="cr-muted">未提供队列数据。</p>'
      }</section>
      <section class="cr-inspection-section" data-cr-diagnostic-budgets><h3>额度与下一可用时间</h3>${
        data.budgets?.length
          ? data.budgets
              .map(
                (budget) =>
                  `<details><summary>${E(budget.title || '工作额度')}${budget.goal_id ? ' · ' + E(identifier(budget.goal_id)) : ''}</summary>${diagnosticFields((budget.counters || []).map((counter) => [counter.label || '额度', supplied(counter.used) + ' / ' + supplied(counter.limit)]))}${diagnosticFields(
                    [
                      ['到期时间', dateTime(budget.expires_at)],
                      ['下一可用时间', dateTime(budget.next_available_at)],
                    ],
                  )}${budget.display?.reason || budget.reason_code ? `<p>${E(budget.display?.reason || budget.reason_code)}</p>` : ''}</details>`,
              )
              .join('')
          : '<p class="cr-muted">未提供额度数据。</p>'
      }</section>
      <section class="cr-inspection-section" data-cr-diagnostic-members><h3>成员与消费证据</h3>${
        data.members?.length
          ? data.members
              .map(
                (member) =>
                  `<details><summary>${E(identifier(member.member_id))} · ${E(stages[member.state] || member.state)}</summary>${diagnosticFields(
                    [
                      ['绑定世代', member.binding_generation],
                      ['订阅状态', member.subscription?.state],
                      ['原生订阅已验证', dateTime(member.subscription_verified_at)],
                      ['探测已读取', dateTime(member.probe_read_at)],
                      ['规范探测已回复', dateTime(member.probe_replied_at)],
                      ['消费者状态', member.consumer?.state],
                      ['消费者 fence', member.consumer?.fence],
                      ['租约截止', dateTime(member.consumer?.lease_until)],
                      ['待处理原输入', member.inbox?.pending_count],
                      ['输入水位', member.inbox?.dirty_revision],
                      ['完成水位', member.inbox?.drained_revision],
                      ['最早输入等待（秒）', member.inbox?.oldest_age_seconds],
                      ['通知待发送', member.notification?.pending_count],
                      ['通知最早等待（秒）', member.notification?.oldest_age_seconds],
                      ['平台接受时间', dateTime(member.notification?.last_accepted_at)],
                      ['下一通知尝试', dateTime(member.notification?.next_attempt_at)],
                    ],
                  )}<p class="cr-muted">平台接受通知不证明模型在线或已经消费。${member.inbox?.complete === false ? '输入列表尚不完整。' : ''}</p></details>`,
              )
              .join('')
          : '<p class="cr-muted">当前没有可显示的成员证据。</p>'
      }</section>
      <section class="cr-inspection-section" data-cr-diagnostic-traces><h3>原记录关联</h3>${
        data.traces?.length
          ? data.traces
              .map(
                (trace) =>
                  `<details data-trace-id="${E(trace.id)}"><summary>${E(traceLabels[trace.kind] || trace.kind)} ${E(identifier(trace.id))} · ${E(trace.display?.title || workLabels[trace.state] || deliveryLabels[trace.state] || trace.state)}</summary>${trace.display?.reason || trace.reason_code ? `<p>${E(trace.display?.reason || trace.reason_code)}</p>` : ''}${diagnosticFields(
                    [
                      ['记录时间', dateTime(trace.updated_at)],
                      ...[
                        ['成员', 'member_id'],
                        ['绑定世代', 'binding_generation'],
                        ['原消息', 'message_id'],
                        ['工作', 'goal_id'],
                        ['步骤', 'work_item_id'],
                        ['原操作', 'operation_id'],
                        ['原投递', 'delivery_id'],
                      ]
                        .filter(([, key]) => trace[key] !== undefined && trace[key] !== '')
                        .map(([label, key]) => [
                          label,
                          key === 'binding_generation' ? trace[key] : identifier(trace[key]),
                        ]),
                    ],
                  )}${trace.kind === 'delivery' ? deliveryMarkup([trace]) : ''}</details>`,
              )
              .join('')
          : '<p class="cr-muted">当前窗口没有可显示的原记录。</p>'
      }</section>
      <details class="cr-diagnostics"><summary>本次观察上限</summary>${diagnosticFields(
        Object.entries(limitLabels)
          .filter(([key]) => data.limits?.[key] !== undefined)
          .map(([key, label]) => [label, data.limits[key]]),
      )}</details>`;
    if (restoreRefresh)
      box.querySelector('[data-cr-action="refresh-diagnostics"]')?.focus({ preventScroll: true });
  }
  async function loadDiagnostics() {
    const current = panel,
      owner = state.owner;
    if (current?.name !== 'diagnostics' || current.loading) return;
    const isCurrent = () =>
      panel === current && state.owner === owner && state.roomId === current.roomId;
    current.loading = true;
    try {
      const data = await api(store.path(current.roomId) + '/diagnostics');
      if (!isCurrent()) return;
      renderDiagnostics(data);
      panelError('');
    } catch (error) {
      if (!isCurrent() || error.name === 'AbortError') return;
      const loading = root.querySelector('[data-cr-diagnostics-loading]');
      if (loading) loading.outerHTML = btn('refresh-diagnostics', '重试读取');
      panelError(error.message);
    } finally {
      current.loading = false;
    }
  }
  function contextCorrectionStale() {
    const correction = store.draft().contextCorrection;
    const context = state.room?.context;
    return !!(
      correction &&
      context &&
      (correction.version !== context.version ||
        correction.visibilityRevision !== context.visibility_revision)
    );
  }
  function renderContext() {
    const box = root?.querySelector('[data-cr-context-view]');
    const value = panel?.context;
    if (!box || panel?.name !== 'context' || !value) return;
    const reasonLabels = {
      unreviewed_input: '还有新的原输入等待核对',
      byte_budget: '本页内容达到大小上限',
      page_limit: '还有事实未载入',
      more_facts: '还有事实未载入',
      history_is_paginated: '完整历史按页读取',
      not_summarized: '原输入尚未形成经核对的摘要',
      source_needs_review: '事实来源需要重新核对',
      recent_messages_omitted: '近期原消息未包含在本页',
      visibility_limited: '当前范围只允许查看部分来源',
    };
    box.innerHTML = `<div class="cr-inspection-heading"><p class="cr-muted">房间要点</p>${btn('refresh-context', '刷新')}</div><p class="cr-muted">点“纠正”后，在输入框写出正确内容。</p>${value.incomplete ? `<p class="cr-info" data-cr-context-incomplete>上下文尚不完整。${E((value.incomplete_reasons || []).map((reason) => reasonLabels[reason] || reason).join('；'))}</p>` : ''}${value.active_projects?.length ? `<p class="cr-muted">当前项目：${E(value.active_projects.map((project) => (typeof project === 'string' ? S.projects.find((item) => item.id === project)?.alias || project : project.project_label || S.projects.find((item) => item.id === project.project_id)?.alias || project.project_id)).join('、'))}</p>` : ''}<div class="cr-context-facts">${value.facts?.length ? value.facts.map((fact) => `<article class="cr-context-fact" data-context-id="${E(fact.id)}"><h3>${E(contextKinds[fact.kind] || fact.kind || '当前事实')}${fact.review_state === 'needs_review' || value.review_state === 'needs_review' ? '<span class="cr-tag">待重新核对</span>' : ''}</h3><p class="cr-context-text">${E(fact.text)}</p>${fact.sources?.length ? `<div class="cr-context-sources" aria-label="事实来源">${fact.sources.map((source) => btn('context-source', source.created_at ? '来源（' + E(time(source.created_at)) + '）' : '查看来源', `data-id="${E(source.message_id)}" data-source-version="${E(source.version)}"`, 'cr-text-button')).join('')}</div>` : '<p class="cr-muted">未提供来源。</p>'}${fact.artifact_refs?.length ? `<p class="cr-muted">产物：${E(fact.artifact_refs.map((artifact) => (typeof artifact === 'string' ? artifact : artifact.name || artifact.label || artifact.id || '原产物引用')).join('、'))}</p>` : ''}${!activeRoom() || value.writable === false || value.archived ? '' : btn('correct-context', '纠正这条事实', `data-id="${E(fact.id)}"`, 'cr-text-button')}</article>`).join('') : '<p class="cr-muted">尚无已核对的当前事实。原消息仍保存在房间中。</p>'}</div>${value.next_cursor ? btn('more-context', '载入更多事实', '', 'cr-full') : ''}`;
  }
  async function loadContext(more = false) {
    const current = panel,
      owner = state.owner;
    if (current?.name !== 'context' || current.loading) return;
    current.loading = true;
    const valid = () =>
      panel === current && state.owner === owner && state.roomId === current.roomId;
    let append = more && !!current.context?.next_cursor;
    const expectedVisibility = state.room?.context?.visibility_revision;
    const firstPage = store.path(current.roomId) + '/context?limit=20&byte_budget=16000';
    try {
      let value;
      try {
        value = await api(
          firstPage + (append ? '&cursor=' + encodeURIComponent(current.context.next_cursor) : ''),
        );
      } catch (error) {
        if (
          !append ||
          !(error.status === 409 || (error.status === 400 && error.code === 'INVALID_CURSOR')) ||
          !valid()
        )
          throw error;
        append = false;
        value = await api(firstPage);
      }
      if (!valid()) return;
      if (
        append &&
        (value.version !== current.context.version ||
          value.visibility_revision !== current.context.visibility_revision)
      ) {
        append = false;
        value = await api(firstPage);
        if (!valid()) return;
      }
      if (append)
        current.context = {
          ...value,
          facts: [
            ...new Map(
              [...current.context.facts, ...(value.facts || [])].map((fact) => [fact.id, fact]),
            ).values(),
          ],
        };
      else {
        store.applyContext(value, expectedVisibility);
        current.context = state.room?.context || value;
      }
      renderContext();
      panelError('');
    } finally {
      current.loading = false;
    }
  }
  async function contextSource(id, sourceVersion) {
    openPanel(
      'context-source',
      '原消息',
      '<div data-cr-context-source><p class="cr-muted">正在核对当前可读原消息…</p></div>',
    );
    const current = panel,
      owner = state.owner;
    const result = await api(
      store.path(current.roomId) + '/messages?message_id=' + encodeURIComponent(id),
    );
    if (panel !== current || state.owner !== owner || state.roomId !== current.roomId) return;
    const message = result.message || result.messages?.find((item) => item.id === id);
    const box = root.querySelector('[data-cr-context-source]');
    if (!message) {
      box.textContent = '原消息当前不可见，请刷新上下文。';
      return;
    }
    box.innerHTML = `<p class="cr-muted">原消息 ${E(identifier(message.id))} · 当前版本 ${E(supplied(message.version))}</p>${Number(sourceVersion) !== Number(message.version) ? '<p class="cr-info">原消息已更新。此处是当前可读版本，摘要引用的旧版本需重新核对。</p>' : ''}<div class="cr-message-body"></div>${message.attachments?.length ? `<div class="cr-message-files">${message.attachments.map(attachmentMarkup).join('')}</div>` : ''}${state.messages.some((item) => item.id === id) ? btn('locate-message', '在房间中查看', `data-id="${E(id)}"`) : ''}${btn('context', '返回当前上下文')}`;
    const body = box.querySelector('.cr-message-body');
    if (typeof chatRichMarkdown === 'function') chatRichMarkdown(body, message.body_text || '');
    else body.textContent = message.body_text || '';
  }
  function settingsPanel() {
    const room = state.room;
    openPanel(
      'settings',
      '房间设置',
      `<form data-cr-form="settings">${field('房间名称', `<input name="title" value="${E(room.title)}" required maxlength="120" autofocus>`)}<button type="submit" class="cr-button cr-primary">保存名称</button></form><section class="cr-setting-actions">${btn('context', '房间要点')}${btn('diagnostics', '运行诊断')}${btn('room-state', room.state === 'paused' ? '继续房间' : '暂停房间', `data-state="${room.state === 'paused' ? 'active' : 'paused'}"`)}${btn('archive-room', room.state === 'archived' ? '恢复房间' : '归档房间', '', 'cr-danger')}</section>`,
      { version: room.version },
    );
  }
  async function workPanel(id) {
    const work = state.works.find((item) => item.id === id);
    openPanel(
      'work',
      work?.title || '当前工作',
      '<div data-cr-work-view><p class="cr-muted">正在读取工作…</p></div>',
      { work: work || { id } },
    );
    state.workDetail = work || { id };
    if (work) renderWork(work);
    const current = panel;
    try {
      await store.work(id);
    } catch (error) {
      if (panel === current && [401, 403, 404].includes(error.status)) {
        root.querySelector('[data-cr-work-view]').textContent =
          '这项工作当前不可查看，请刷新房间。';
        state.works = state.works.filter((item) => item.id !== id);
        patchWorks();
      }
      throw error;
    }
    if (panel !== current) return;
    const latest =
      state.works.find((item) => item.id === id) ||
      (state.workDetail?.id === id ? state.workDetail : null);
    if (latest) renderWork(latest);
  }
  function workRecoveryForm(action, candidates) {
    const takeover = action === 'takeover';
    const heading = takeover ? '接替这项工作的主责' : '恢复原成员的当前连接';
    return `<form class="cr-work-recovery" data-cr-form="work-recovery" data-work-action="${action}"><h3>${heading}</h3><p class="cr-muted">${takeover ? '只可选择这项工作原已批准且当前范围有效的成员。接替仅作用于此工作。' : '在同一工作中核对并恢复所选成员的连接。原操作及其结果继续保留，不自动重跑。'}</p>${field(takeover ? '接替成员' : '恢复成员', `<select name="target_member_id" required><option value="">请选择成员</option>${candidates.map((member) => `<option value="${E(member.member_id)}">${E(member.name || 'dot')} · #${E(member.member_id.slice(0, 8))}</option>`).join('')}</select>`)}<div class="cr-muted" data-cr-recovery-scope></div><button type="submit" class="cr-button cr-primary">${takeover ? '确认接替原工作' : '确认恢复原工作'}</button></form>`;
  }
  function renderWork(work) {
    if (panel?.name !== 'work') return;
    const box = root?.querySelector('[data-cr-work-view]');
    if (!box) return;
    const actions = (work.allowed_actions || work.display?.allowed_actions || []).filter(
      (action) => activeRoom() || safeWorkActions.has(action),
    );
    const recovery = work.recovery || {};
    const coordinatorId = work.coordinator_member_id || recovery.coordinator_member_id;
    const team = work.members || [];
    const coordinator = team.find((member) => (member.member_id || member.id) === coordinatorId);
    const operations = work.operations || [];
    const workProject = (work.project_ids || []).find((id) =>
      S.projects.some((project) => project.id === id),
    );
    const reviewRevision = JSON.stringify([
      activeRoom(),
      work.version,
      work.intent_revision,
      work.coordinator_epoch || recovery.coordinator_epoch,
      recovery.restore_candidates || [],
      recovery.takeover_candidates || [],
    ]);
    const changedReview = panel.workReviewRevision && panel.workReviewRevision !== reviewRevision;
    const selected = [...box.querySelectorAll('form[data-cr-form="work-recovery"]')].map(
      (form) => ({ action: form.dataset.workAction, value: form.querySelector('select').value }),
    );
    const focusedAction = document.activeElement?.closest?.('[data-cr-form="work-recovery"]')
      ?.dataset.workAction;
    panel.work = work;
    const markup = `<div class="cr-flow-state"><h3>${E(work.display?.title || workLabels[work.state] || work.state)}</h3><p>${E(work.display?.reason || work.reason_code || '')}</p></div>${!activeRoom() ? '<p class="cr-info">房间已暂停或归档。可以查看历史，也可以停止已有工作。</p>' : ''}${work.result_summary ? `<p>${E(work.result_summary)}</p>` : ''}${deliveryMarkup(work.channel_deliveries, work.delivery_display, work.channel_deliveries_truncated)}
      <section class="cr-work-summary">${work.acceptance ? `<h3>完成条件</h3><p>${E(work.acceptance)}</p>` : ''}${coordinatorId ? `<p data-cr-work-coordinator><strong>当前主责</strong> ${E(coordinator?.name || recovery.coordinator_name || 'dot')}</p>` : ''}${team.length ? `<h3>参与成员</h3><ul>${team.map((member) => `<li>${E(member.name || 'dot')}${(member.member_id || member.id) === coordinatorId ? ' · 主责' : ''}</li>`).join('')}</ul>` : ''}</section>
      <details class="cr-diagnostics cr-work-evidence"><summary>步骤与执行记录</summary>${work.items?.length ? `<ul>${work.items.map((item) => `<li><strong>${E(item.title || '工作步骤')}</strong> · ${E(workLabels[item.state] || item.state)}${item.display?.reason || item.reason_code ? `<p>${E(item.display?.reason || item.reason_code)}</p>` : ''}</li>`).join('')}</ul>` : '<p class="cr-muted">当前没有可显示的执行步骤。</p>'}<div data-cr-operation-receipts>${operations.length ? `<ul>${operations.map((operation) => `<li data-operation-id="${E(operation.id || operation.operation_id)}"><strong>${E(operation.display?.title || workLabels[operation.state] || operation.state)}</strong>${operation.cancel_requested && !['succeeded', 'failed', 'cancelled'].includes(operation.state) ? '<p>已请求停止，等待原操作回执</p>' : ''}${operation.display?.reason ? `<p>${E(operation.display.reason)}</p>` : ''}</li>`).join('')}</ul>` : '<p class="cr-muted">还没开始执行。</p>'}</div></details>
      ${recovery.state === 'waiting_operations' ? '<p class="cr-info">等当前操作结束后才能恢复或接替。</p>' : ''}
      ${actions.includes('restore_binding') && recovery.restore_candidates?.length ? workRecoveryForm('restore_binding', recovery.restore_candidates) : ''}
      ${actions.includes('takeover') && recovery.takeover_candidates?.length ? workRecoveryForm('takeover', recovery.takeover_candidates) : ''}
      ${workProject ? `<button type="button" class="cr-button cr-text-button" data-wf-action="project" data-project="${E(workProject)}">查看项目任务进度</button>` : ''}
      <div class="cr-work-controls">${actions
        .filter((action) => ['pause', 'resume', 'cancel', 'stop'].includes(action))
        .map((action) =>
          btn(
            'work-control',
            { pause: '暂停工作', resume: '继续原工作', cancel: '停止工作', stop: '停止工作' }[
              action
            ],
            `data-work-action="${action}"`,
            ['cancel', 'stop'].includes(action) ? 'cr-danger' : 'cr-primary',
          ),
        )
        .join('')}${actions.includes('supplement') ? btn('supplement', '补充说明') : ''}</div>`;
    if (box._markup !== markup || changedReview) {
      box.innerHTML = markup;
      box._markup = markup;
      if (!changedReview)
        for (const saved of selected) {
          const form = [...box.querySelectorAll('form')].find(
            (item) => item.dataset.workAction === saved.action,
          );
          if (form) {
            form.querySelector('select').value = saved.value;
            updateWorkRecoveryScope(form);
          }
        }
      if (focusedAction)
        [...box.querySelectorAll('form')]
          .find((form) => form.dataset.workAction === focusedAction)
          ?.querySelector('select')
          ?.focus({ preventScroll: true });
    }
    panel.workReviewRevision = reviewRevision;
    if (changedReview && selected.some((item) => item.value))
      panelError('工作或连接范围已变化，请查看结果后重新选择成员。');
  }
  function updateWorkRecoveryScope(form) {
    const action = form.dataset.workAction;
    const candidates =
      panel?.work.recovery?.[
        action === 'takeover' ? 'takeover_candidates' : 'restore_candidates'
      ] || [];
    const selected = candidates.find(
      (member) => member.member_id === form.querySelector('select').value,
    );
    const scope = form.querySelector('[data-cr-recovery-scope]');
    scope.textContent = selected
      ? (selected.projects || [])
          .map(
            (project) =>
              `${S.projects.find((item) => item.id === project.project_id)?.alias || project.project_id} · ${(project.capabilities || []).map((capability) => ({ read: '读取', write: '修改', execute: '执行' })[capability] || capability).join('、')}${project.execution_targets?.length ? ' · ' + project.execution_targets.map((target) => (target === 'project_agent' ? '项目 Agent' : target)).join('、') : ''}`,
          )
          .join('；')
      : '';
  }
  async function controlWork(action, targetMemberId) {
    const work = panel.work;
    if (!(work.allowed_actions || work.display?.allowed_actions || []).includes(action)) {
      panelError('原工作当前不允许此动作，请重新读取后核对。');
      return;
    }
    const result = await mutation(
      'POST',
      base + '/work/' + encodeURIComponent(work.id) + '/control',
      {
        action,
        expected_version: work.version,
        expected_intent_revision: work.intent_revision,
        ...(work.coordinator_epoch || work.recovery?.coordinator_epoch
          ? {
              expected_coordinator_epoch: work.coordinator_epoch || work.recovery.coordinator_epoch,
            }
          : {}),
        ...(targetMemberId ? { target_member_id: targetMemberId } : {}),
      },
    );
    if (result) {
      store.applyWork({
        ...result.work,
        operations: result.work.operations || result.operations || [],
      });
      workPanel(work.id);
    }
  }
  async function weixinPanel() {
    openPanel(
      'weixin',
      '连接微信',
      '<div data-cr-channel-view><p class="cr-muted">正在读取连接状态…</p></div>',
    );
    const current = panel;
    if (!activeRoom()) {
      const existing = state.room?.channels?.find(
        (channel) => channel.kind === 'weixin' && channel.flow_id,
      );
      if (existing) {
        const result = await api(
          base + '/channel-connect-flows/' + encodeURIComponent(existing.flow_id),
        );
        if (panel === current) renderChannel(result);
      } else {
        const legacyChannels = (state.room?.migration?.blockers || []).filter(
          (blocker) => blocker.code === 'LEGACY_CHANNEL_ACTIVE',
        );
        root.querySelector('[data-cr-channel-view]').innerHTML = state.room?.legacy
          ? `<ol class="cr-steps" aria-label="连接微信进度"><li class="is-current" aria-current="step"><span>!</span>先完成房间迁移</li><li><span>1</span>登录微信</li><li><span>2</span>本人验证</li></ol><div class="cr-wx-head"><span class="cr-legacy-icon" aria-hidden="true">!</span><div><h3>迁移后就能连接微信</h3><p>这个房间还在旧版协作中心，此处不会创建新的微信接入或批准工作许可。完成迁移条目并启用新房间界面后，回到这里扫码；之前登记过的微信可以直接沿用。</p></div></div>${legacyChannels.length ? `<p class="cr-info">这个房间还有 ${legacyChannels.length} 条旧渠道连接需要先暂停。暂停会保留账号、历史和投递回执。</p><ul class="cr-wx-legacy">${legacyChannels.map((item) => migrationBlockerMarkup(item, true)).join('')}</ul>` : ''}${btn('migration', '查看旧连接与迁移要求', '', 'cr-primary cr-full')}`
          : `<p class="cr-info">${E(readOnlyReason())}</p><p>此处不会创建新的微信接入或批准工作许可。</p>${btn('settings', '查看房间状态')}`;
      }
      return;
    }
    const existing = state.room?.channels?.find(
      (channel) =>
        channel.kind === 'weixin' &&
        channel.flow_id &&
        !['closed', 'cancelled', 'disconnected'].includes(channel.stage),
    );
    if (existing) {
      const result = await api(
        base + '/channel-connect-flows/' + encodeURIComponent(existing.flow_id),
      );
      if (panel === current) renderChannel(result);
    } else {
      // Opening the local flow does not start QR login. That needs its own consent.
      await startWeixin(current);
    }
  }
  async function startWeixin(current) {
    const result = await mutation(
      'POST',
      store.path() + '/channels/weixin/connect',
      {
        ...(state.room.lead_member_id ? { member_id: state.room.lead_member_id } : {}),
      },
      current,
    );
    if (result && panel === current) renderChannel(result);
  }

  function inboundHealthMarkup(accounts) {
    const items = (accounts || []).flatMap((account) =>
      (account.inbound_health || [])
        .filter(
          (item) => ['deferred', 'quarantined'].includes(item.state) && Number(item.count) > 0,
        )
        .map(
          (item) =>
            `<li><strong>${E(account.label || account.bot_id || '当前账号')}</strong> · ${item.state === 'deferred' ? '等待积压消化' : '已隔离'} ${E(item.count)} 条 · <code>${E(item.reason_code || 'unknown')}</code></li>`,
        ),
    );
    return items.length
      ? `<section class="cr-info" aria-label="渠道收件状态"><strong>部分消息需要留意</strong><ul>${items.join('')}</ul><p>延后消息保留在原连接，积压消化后自动核对；隔离消息不会执行任务。后续正常消息仍可接收。</p></section>`
      : '';
  }
  function uploadEstimateMarkup(estimate) {
    if (!estimate || !Number.isFinite(Number(estimate.estimated_seconds))) return '';
    const seconds = Math.max(0, Math.ceil(Number(estimate.estimated_seconds)));
    const duration = seconds >= 60 ? Math.ceil(seconds / 60) + ' 分钟' : seconds + ' 秒';
    return `<p class="cr-muted" data-cr-upload-estimate>附件外发参考：${estimate.reference_bytes ? E(fileSize(estimate.reference_bytes)) + ' 文件' : '所选文件'}约 ${E(duration)}。 ${E(estimate.note || '')} 外发文件仍需单独批准。</p>`;
  }
  const channelSteps = [
    ['扫码', ['choose_account', 'scan', 'confirm_account', 'needs_reconnect']],
    [
      '确认范围',
      [
        'choose_member',
        'waiting_member',
        'waiting_project',
        'route_conflict',
        'confirm_binding',
        'pairing',
        'pairing_expired',
        'confirm_owner',
      ],
    ],
    ['完成', ['ready', 'paused', 'disconnected', 'closed', 'cancelled']],
  ];
  const channelCopy = {
    choose_account: ['连接你的微信', '用手机微信扫码登录，或选择已登记的微信。'],
    scan: ['用手机微信扫一扫', '扫码后在手机上确认登录。二维码 5 分钟内有效，本页会自动刷新状态。'],
    confirm_account: [
      '确认扫码的是你',
      '核对手机返回的扫码身份。确认后加密保存这个微信的登录凭据。',
    ],
    needs_reconnect: [
      '微信需要重新登录',
      '原登录已过期或被微信拒绝。重新扫码后沿用原账号和原房间连接。',
    ],
    choose_member: [
      '选择接收微信消息的 dot',
      '房间里有多个 dot 但还没有主责。微信消息会交给这里选择的 dot 处理和回复。',
    ],
    waiting_member: [
      '先为房间添加一个 dot',
      '微信消息需要一个 dot 来接收和回复。添加并连上 dot 后回到这里继续。',
    ],
    waiting_project: ['先为原连接关联项目', '旧连接需要先关联一个项目才能继续。'],
    route_conflict: ['先解除原来的微信连接', ''],
    confirm_binding: [
      '确认这个房间的私聊范围',
      '允许你本人微信私聊里的消息进入这个房间；也可以选择允许你在微信里直接交办工作。',
    ],
    pairing: ['在微信里发送配对码', '把配对码发到机器人私聊，这里会自动继续。'],
    pairing_expired: ['配对码已过期', '配对码 15 分钟内有效。生成新的配对码后再发送一次。'],
    confirm_owner: ['确认是你本人发送的', '已收到配对码。核对发送者后完成连接。'],
    ready: ['微信已连接', ''],
    paused: ['微信已暂停', '暂停期间不接收新的微信消息。恢复后沿用原联系人和原房间。'],
    disconnected: ['微信已断开', '可以重新确认连接范围后再次配对。'],
    cancelled: ['已取消连接', '可以随时重新开始。'],
    closed: ['微信已解除', '历史消息保留在房间。'],
  };
  const channelReasons = {
    WEIXIN_QR_EXPIRED: '二维码已过期，请重新生成。',
    WEIXIN_QR_ATTENTION: '微信没有返回可用的扫码结果，请重新生成二维码。',
    WEIXIN_QR_CANCELLED: '上一次扫码已取消。',
    WEIXIN_QR_RESPONSE_UNCONFIRMED: '暂时连不上微信扫码服务，请稍后重新生成二维码。',
    WEIXIN_QR_RESPONSE_UNSUPPORTED: '微信返回了无法识别的扫码结果，请重新生成二维码。',
    WEIXIN_CREDENTIAL_CHANGED: '微信登录凭据已变化，请重新扫码。',
    WEIXIN_ACCOUNT_CONFLICT: '这个微信已由其他账号登记，不能在这里接入。',
    WEIXIN_SESSION_EXPIRED: '微信登录已过期，请重新扫码。',
    WEIXIN_AUTH_REJECTED: '微信拒绝了当前登录凭据，请重新扫码。',
    WEIXIN_RENEWAL_BOT_MISMATCH: '扫码的不是原来的微信，请用原微信重新扫码。',
    WEIXIN_DISABLED: '空间管理员已关闭微信接入。',
  };
  const quietInbound = new Set([
    '',
    'WEIXIN_POLL_IDLE',
    'WEIXIN_POLL_FENCE_LOST',
    'WEIXIN_BINDING_CHANGED',
    'WEIXIN_POLL_CANCELLED',
    'WEIXIN_PAIRING_EXPIRED',
  ]);
  const safeImage = (value) =>
    typeof value === 'string' &&
    value.length <= 1500000 &&
    /^data:image\/svg\+xml;base64,[A-Za-z0-9+/]+={0,2}$/.test(value);
  const reasonText = (code) =>
    !code ? '' : channelReasons[code] || (/^[A-Z0-9_]+$/.test(code) ? '状态代码：' + code : code);
  const facts = (rows) =>
    `<dl class="cr-wx-facts">${rows
      .filter(([, value]) => value)
      .map(([name, value]) => `<div><dt>${E(name)}</dt><dd>${E(value)}</dd></div>`)
      .join('')}</dl>`;
  function channelLiveParts(box, flow) {
    // Clock-driven text is patched in place so a poll never re-renders the
    // form (and never clears boxes the owner has already ticked).
    const inbound = box.querySelector('[data-cr-inbound]');
    if (inbound) {
      const value = flow.inbound || {};
      const code = value.reason_code || '';
      const problem = !quietInbound.has(code);
      const age = value.last_sync_at
        ? Math.max(0, Math.round(Date.now() / 1000 - Number(value.last_sync_at)))
        : null;
      inbound.className = 'cr-inbound ' + (problem ? 'is-problem' : age !== null ? 'is-live' : '');
      inbound.textContent = problem
        ? '收件通道异常：' + reasonText(code)
        : age === null
          ? '正在连接微信收件通道…'
          : '收件通道正常 · ' +
            (age < 60 ? Math.max(1, age) + ' 秒前同步' : Math.round(age / 60) + ' 分钟前同步');
    }
    const countdown = box.querySelector('[data-cr-countdown]');
    if (countdown) {
      const left = Math.max(0, Math.round(Number(countdown.dataset.until) - Date.now() / 1000));
      countdown.textContent = left
        ? String(Math.floor(left / 60)).padStart(2, '0') + ':' + String(left % 60).padStart(2, '0')
        : '已到期';
    }
  }
  function channelIdentity(nickname, id) {
    if (nickname) return nickname;
    const value = String(id || '');
    return value.length > 8
      ? value.slice(0, 4) + '…' + value.slice(-4)
      : value
        ? value.slice(0, 1) + '…'
        : '';
  }
  function channelScopeMarkup(preview, flow) {
    const projects =
      preview.projects ||
      preview.project_scopes ||
      preview.scope?.project_scopes ||
      (preview.project_id ? [preview] : []);
    const following = (preview.scope?.routing_mode || preview.routing_mode) === 'follow_room';
    const member = (state.room?.members || []).find(
      (item) => memberId(item) === state.room.lead_member_id,
    );
    const renewal = member?.subscription_renewal || flow.member?.subscription_renewal;
    const paired = flow.stage === 'ready';
    return `<section class="cr-connect-summary" aria-label="微信交办授权摘要">
      <h3>微信交办授权</h3>
      ${facts([
        ['房间', roomName(state.room)],
        [
          '本人微信',
          paired
            ? channelIdentity(flow.binding?.peer_nickname, flow.binding?.peer_id) ||
              '当前已配对联系人'
            : '发送配对码并由房主确认的联系人',
        ],
        ['新消息交给', following ? '房间当前主责 dot' : flow.member?.name || '已指定的 dot'],
        ['回传', '当前房间和发起任务的原私聊'],
        [
          '连接保持',
          renewal?.mode === 'managed'
            ? 'CodePier 定期保持连接'
            : renewal?.mode === 'host'
              ? '由 dot 宿主负责续期'
              : null,
        ],
        [
          '连接期限',
          renewal?.approved_until
            ? dateTime(renewal.approved_until)
            : renewal?.expires_at
              ? dateTime(renewal.expires_at)
              : null,
        ],
      ])}
      ${
        projects.length
          ? `<ul>${projects
              .map((project) => {
                const name =
                  project.project_label ||
                  project.alias ||
                  S.projects.find((item) => item.id === project.project_id)?.alias ||
                  project.project_id ||
                  project;
                const caps = (project.capabilities || []).map(
                  (cap) => ({ read: '读取', write: '修改', execute: '执行' })[cap] || cap,
                );
                const expires = project.lifetime?.expires_at || project.expires_at;
                return `<li>${E(name)}${caps.length ? ' · ' + E(caps.join('、')) : ''}${expires ? ' · 至 ' + E(dateTime(expires)) : ''}${project.execution_targets?.length ? '<br>执行位置：' + E(project.execution_targets.map((target) => (target === 'project_agent' ? '项目 Agent' : target)).join('、')) : ''}</li>`;
              })
              .join('')}</ul>`
          : '<p class="cr-muted">当前没有可交办的项目，私聊可以正常收发消息。</p>'
      }
      <p class="cr-muted">配对后可在此关闭交办工作，或在权限设置中调整项目与能力。</p>
    </section>`;
  }
  function renderChannel(result) {
    if (!panel || !root) return;
    const flow = result.flow || result;
    panel.channel = flow;
    panel.flowId = flow.id;
    const box = root.querySelector('[data-cr-channel-view]');
    if (!box) return;
    const next = flow.next_request || {};
    const serverFields = next.form_fields || [];
    const isFollowConfirmation = (item) => (item.name || item.key) === 'confirm_follow_room';
    const fields = serverFields.filter((item) => !isFollowConfirmation(item));
    const action = next.body?.action || flow.primary_action;
    const followPreview = ['bind', 'approve_tasks'].includes(action)
      ? flow.follow_task_scope
      : null;
    const allowedActions = flow.allowed_actions || (flow.actions || []).map((item) => item.action);
    const writable = activeRoom();
    const stage = flow.stage;
    const accounts =
      writable && stage === 'choose_account' && allowedActions.includes('select_account')
        ? flow.accounts || []
        : [];
    const label =
      flow.actions?.find((item) => item.action === action)?.label || actionLabel(action);
    const current = channelSteps.findIndex(([, stages]) => stages.includes(stage));
    const existing = flow.existing_binding;
    const [heading, copy] = channelCopy[stage] || [flow.display?.title || title(flow), ''];
    const lead =
      stage === 'route_conflict' && existing
        ? existing.same_room
          ? `这个房间还保留着一条${existing.legacy ? '旧版' : ''}微信连接（${existing.state === 'paused' ? '已暂停' : '仍在使用'}）。解除后才能绑定新的连接；原消息和投递回执会保留。`
          : `这个微信目前连接在「${existing.room_title ? roomName({ title: existing.room_title }) : '另一个房间'}」。一个私聊只能连接一个房间，确认后新消息进入当前房间，原房间的历史保留。`
        : stage === 'ready'
          ? flow.work_authorized
            ? '已允许本人交办工作：你在微信里提出的明确请求，会在批准范围内自动转成工作。'
            : '已连接房间聊天：微信私聊的消息会进入这个房间，dot 的回复会发回私聊。'
          : copy;
    const reason = reasonText(flow.reason_code || flow.display?.reason);
    const parts = [
      `<ol class="cr-steps" aria-label="连接微信进度">${channelSteps
        .map(
          ([name], index) =>
            `<li class="${index < current || (index === current && stage === 'ready') ? 'is-done' : index === current ? 'is-current' : ''}" ${index === current ? 'aria-current="step"' : ''}><span>${index < current || stage === 'ready' ? '✓' : index + 1}</span>${E(name)}</li>`,
        )
        .join('')}</ol>`,
      `<div class="cr-wx-head ${stage === 'ready' ? 'is-ready' : ''}"><span class="cr-flow-symbol ${stage === 'ready' ? 'is-ready' : ''}" aria-hidden="true">${stage === 'ready' ? '✓' : icon('message')}</span><div><h3>${E(heading)}</h3>${lead ? `<p>${E(lead)}</p>` : ''}</div></div>`,
    ];
    if (reason && !['ready', 'pairing'].includes(stage))
      parts.push(`<p class="cr-warning" role="status">${E(reason)}</p>`);
    if (!writable)
      parts.push(`<p class="cr-info">${E(readOnlyReason())} 此处仅保留安全暂停或撤销操作。</p>`);
    if (accounts.length)
      parts.push(
        `<section class="cr-wx-option cr-channel-existing"><h4>沿用已登记的微信</h4>${field('已连接的微信', `<select name="existing_account_id"><option value="">选择账号</option>${accounts.map((account) => `<option value="${E(account.id)}">${E(account.label || channelIdentity(null, account.bot_id))}</option>`).join('')}</select>`)}${btn('channel-existing', '使用此账号', '', 'cr-full')}</section><p class="cr-or"><span>或者扫码登录新的微信</span></p>`,
      );
    parts.push(inboundHealthMarkup(flow.account ? [flow.account] : accounts));
    const qr = flow.qr || {};
    if (safeImage(qr.qrcode_image))
      parts.push(
        `<figure class="cr-qr-frame"><img class="cr-qr" src="${E(qr.qrcode_image)}" alt="用微信扫描二维码登录" referrerpolicy="no-referrer"><figcaption>${qr.state === 'scaned' ? '已扫码，请在手机上点“确认登录”' : qr.state === 'need_verifycode' ? '请在下方输入手机上显示的数字' : '打开手机微信 → 右上角 ＋ → 扫一扫'}</figcaption></figure>`,
      );
    if (stage === 'confirm_account' && (qr.re_pair_required || qr.account_conflict))
      parts.push(
        facts([
          ['微信', channelIdentity(qr.scanner_nickname, qr.scanner_id)],
          ['已登记账号', qr.existing_account?.label],
        ]) +
          (qr.re_pair_required
            ? '<p class="cr-info">这个微信之前登记过。确认后沿用原账号；它在其他房间的连接需要重新确认。</p>'
            : ''),
      );
    if (stage === 'route_conflict' && existing)
      parts.push(
        facts([
          ['原连接', identifier(existing.id)],
          [
            '所在房间',
            existing.same_room
              ? '当前房间'
              : roomName({ title: existing.room_title }) || '另一个房间',
          ],
          [
            '状态',
            { paused: '已暂停', active: '使用中', pairing: '等待配对', awaiting_owner: '等待确认' }[
              existing.state
            ] || existing.state,
          ],
        ]),
      );
    if (['confirm_binding', 'choose_member'].includes(stage))
      parts.push(
        facts([
          ['房间', roomName(state.room)],
          ['接收消息的 dot', flow.member?.name],
          ['微信', flow.account?.label || channelIdentity(null, flow.account?.bot_id)],
        ]),
      );
    if (stage === 'waiting_member' && writable)
      parts.push(btn('add-member', '添加 dot', '', 'cr-primary cr-full'));
    const binding = flow.binding || {};
    if (binding.pairing_challenge) {
      parts.push(
        `<section class="cr-pair" aria-label="配对码"><div class="cr-pair-code"><code class="cr-pairing-code">${E(binding.pairing_challenge)}</code>${btn('copy-pairing', '复制', 'aria-label="复制配对码"', 'cr-copy')}</div>${safeImage(binding.pairing_qr) ? `<figure class="cr-pair-qr"><img src="${E(binding.pairing_qr)}" alt="配对码二维码：用手机扫码后可直接复制" referrerpolicy="no-referrer"><figcaption>手机扫一扫，可直接复制配对码</figcaption></figure>` : ''}<ol class="cr-pair-steps"><li>在手机微信里打开与这个微信机器人的私聊。</li><li>把配对码原样发过去（大小写、空格都没关系）。</li><li>收到后这里会自动进入下一步。</li></ol><p class="cr-muted">剩余有效时间 <span data-cr-countdown data-until="${E(binding.pairing_until || 0)}"></span></p></section>`,
      );
      if (flow.pairing_feedback)
        parts.push(
          `<p class="cr-warning" role="status">${flow.pairing_feedback.state === 'different_peer' ? '收到的配对码来自另一位联系人。恢复连接必须由原联系人发送。' : '收到了一条微信消息，但不是当前配对码。请复制完整的配对码再发一次。'}</p>`,
        );
    }
    if (stage === 'confirm_owner')
      parts.push(
        facts([
          ['联系人', channelIdentity(binding.pending_peer_nickname, binding.pending_peer_id)],
          ['房间', roomName(state.room)],
        ]),
      );
    if (['pairing', 'confirm_owner', 'ready', 'confirm_binding'].includes(stage) && flow.inbound)
      parts.push('<p class="cr-inbound" data-cr-inbound></p>');
    if (stage === 'ready')
      parts.push(
        facts([
          ['微信', flow.account?.label || channelIdentity(null, flow.account?.bot_id)],
          ['房间', roomName(state.room)],
          ['本人配对', '已验证'],
          [
            '新消息交给',
            flow.routing_mode === 'follow_room'
              ? '房间当前主责 dot'
              : flow.member?.name || '已指定的 dot',
          ],
        ]) +
          `<p class="cr-wx-toggle">在微信里交办工作：${flow.work_authorized ? '已允许' : '未开启'} ${flow.work_authorized ? btn('channel-action', '关闭', 'data-channel-action="revoke_tasks" title="保留聊天连接" aria-label="关闭微信交办工作"', 'cr-text-button') : ''}</p>` +
          btn('channel-project-permissions', '修改权限', '', 'cr-text-button'),
      );
    if ((flow.task_scope || followPreview) && (stage !== 'ready' || action === 'approve_tasks'))
      parts.push('<div data-cr-wx-scope></div>');
    const showForm =
      action &&
      stage !== 'pairing' &&
      (stage !== 'confirm_account' || qr.re_pair_required || qr.account_conflict) &&
      (writable || safeChannelActions.has(action)) &&
      (stage !== 'ready' || !['refresh', ''].includes(action));
    if (showForm) {
      const bindingConsent = action === 'bind';
      const startConsent = action === 'start_qr';
      const optional = fields.filter((item) => (item.name || item.key) === 'allow_owner_tasks');
      const basic = bindingConsent
        ? fields.filter((item) => ['allow_replies'].includes(item.name))
        : startConsent
          ? []
          : fields.filter((item) => !optional.includes(item));
      const explanation = bindingConsent
        ? '<p>发送配对码的微信将连到此房间，直到你解除。请用本人微信发送。如果开启交办工作，后续请求按上面所列权限执行，结果发回该私聊。</p>'
        : startConsent
          ? '<p>点击扫码即同意使用微信机器人服务，并加密保存扫码返回的登录凭据。</p>'
          : '';

      const quiet =
        ['pairing', 'waiting_member', 'waiting_project'].includes(stage) && action === 'refresh';
      const followRequired =
        serverFields.find(isFollowConfirmation)?.required === true ||
        (action === 'approve_tasks' &&
          !flow.task_scope &&
          followPreview?.project_ids?.length === 0);
      const follow = followPreview
        ? `<label class="cr-choice"><input type="checkbox" name="confirm_follow_room" ${followRequired ? 'required' : ''}><span>跟随房间主责<small>房主更换主责后，新消息交给新主责处理。</small></span></label>`
        : '';
      parts.push(
        `<form data-cr-form="weixin" data-channel-action="${E(action)}" class="cr-wx-form">${explanation}${basic.map(channelField).join('')}${optional.length ? `<fieldset class="cr-wx-optional"><legend>可选：在微信里交办工作</legend>${optional.map(channelField).join('')}</fieldset>` : ''}${follow}<button type="submit" class="cr-button ${quiet ? '' : 'cr-primary'} cr-full">${E(quiet ? '刷新状态' : bindingConsent ? '确认范围并生成配对码' : label)}</button></form>`,
      );
    }
    const secondary = (flow.actions || []).filter(
      (item) =>
        item.action !== action &&
        !(stage === 'ready' && item.action === 'revoke_tasks') &&
        (writable || safeChannelActions.has(item.action)),
    );
    if (secondary.length)
      parts.push(
        `<div class="cr-setting-actions cr-wx-secondary">${secondary.map((item) => btn('channel-action', E(item.action === 'revoke_tasks' ? '仅撤销工作许可' : item.label), `data-channel-action="${E(item.action)}"`, ['disconnect', 'revoke_tasks'].includes(item.action) ? 'cr-danger' : 'cr-text-button')).join('')}</div>`,
      );
    const markup = parts.join('');
    const consentRevision = JSON.stringify([
      writable,
      flow.id,
      flow.version,
      flow.account?.id,
      flow.account?.credential_version,
      binding.id,
      binding.version,
      binding.generation,
      binding.pending_peer_id,
      flow.task_scope?.scope_digest,
      flow.follow_task_scope?.scope_digest,
      flow.routing_mode,
      qr.scanner_id,
      qr.re_pair_required,
      existing?.id,
      existing?.version,
    ]);
    const changedConsent = box._consentRevision && box._consentRevision !== consentRevision;
    const checkedBeforeChange =
      changedConsent && !!box.querySelector('input[type="checkbox"]:checked');
    if (box._markup !== markup || changedConsent) {
      box.innerHTML = markup;
      box._markup = markup;
    }
    box._consentRevision = consentRevision;
    channelLiveParts(box, flow);
    if (checkedBeforeChange) panelError('连接身份或参与范围已变化，请核对后重新确认。');
    updateChannelDependencies();
    patchRoomPermissions();
  }
  function updateChannelDependencies() {
    const form = root?.querySelector('[data-cr-form="weixin"]');
    for (const control of form?.querySelectorAll('[data-depends-on]') || []) {
      const enabled = form.elements.namedItem(control.dataset.dependsOn)?.checked === true;
      control.disabled = !enabled;
      control.required = enabled;
      control.closest('label').hidden = !enabled;
      if (!enabled) control.checked = false;
    }
    const scopeBox = root.querySelector('[data-cr-wx-scope]');
    if (scopeBox) {
      const flow = panel.channel;
      const follow = form?.elements.namedItem('confirm_follow_room')?.checked;
      const preview = follow || !flow.task_scope ? flow.follow_task_scope : flow.task_scope;
      const markup = preview ? channelScopeMarkup(preview, flow) : '';
      if (scopeBox._markup !== markup) {
        scopeBox.innerHTML = markup;
        scopeBox._markup = markup;
      }
    }
  }
  function channelField(item) {
    const name = item.name || item.key,
      label = item.label || name;
    if (!name) return '';
    // Identity confirmations carry the exact value the server showed above;
    // they are confirmed by submitting, not retyped.
    if (['confirm_scanner_id', 'confirm_peer_id', 'confirm_migrate_binding_id'].includes(name))
      return `<input type="hidden" name="${E(name)}" value="${E(item.value || '')}">`;
    if (item.options)
      return field(
        E(label),
        `<select name="${E(name)}" ${item.required ? 'required' : ''}>${item.options.map((option) => `<option value="${E(option.value ?? option.id)}">${E(option.label || option.name)}</option>`).join('')}</select>`,
      );
    if (item.type === 'checkbox' || item.type === 'boolean')
      return `<label class="cr-choice"><input type="checkbox" name="${E(name)}" ${name === 'allow_replies' ? 'checked' : item.required ? 'required' : ''} ${item.depends_on ? `data-depends-on="${E(item.depends_on)}"` : ''}><span>${E(label)}</span></label>`;
    return field(
      E(label),
      `<input name="${E(name)}" value="${E(item.value || '')}" ${item.required ? 'required' : ''} autocomplete="off" ${name === 'verify_code' ? 'inputmode="numeric"' : ''}>`,
    );
  }
  async function submitChannel(form) {
    const flow = panel.channel,
      next = flow.next_request || {},
      values = Object.fromEntries(new FormData(form));
    for (const input of form.querySelectorAll('input[type="checkbox"]')) {
      if (input.name !== 'confirm_follow_room' || input.checked) values[input.name] = input.checked;
      else delete values[input.name];
    }
    if (form.dataset.channelAction === 'start_qr')
      Object.assign(values, { confirm_credential_storage: true, confirm_provider_terms: true });
    if (form.dataset.channelAction === 'bind') {
      Object.assign(values, {
        confirm_external_data_ingress: true,
        confirm_until_revoked: true,
        confirm_pairing_sender: true,
      });
      if (values.allow_owner_tasks) {
        if (!values.allow_replies) {
          panelError('在微信里交办工作需要允许回复回到本人微信。');
          return;
        }
        Object.assign(values, {
          confirm_owner_identity: true,
          confirm_future_tasks: true,
          confirm_result_delivery: true,
          confirm_pairing_notices: true,
        });
      }
    }
    const taskScope = values.confirm_follow_room ? flow.follow_task_scope : flow.task_scope;
    const nextBody = { ...(next.body || {}) };
    delete nextBody.confirm_follow_room;
    const body = {
      ...nextBody,
      ...values,
      action: form.dataset.channelAction,
      expected_version: flow.version,
      ...(taskScope?.scope_digest ? { expected_scope_digest: taskScope.scope_digest } : {}),
      ...(flow.existing_binding ? { expected_binding_version: flow.existing_binding.version } : {}),
    };
    const result = await mutation(
      'POST',
      base + '/channel-connect-flows/' + encodeURIComponent(flow.id) + '/actions',
      body,
    );
    if (result) {
      renderChannel(result);
      if ((result.flow || result).stage === 'ready') await store.refresh();
    }
  }
  async function refreshChannel() {
    if (!panel?.flowId || panel.busy) return;
    const current = panel,
      epoch = panelEpoch,
      writeEpoch = current.writeEpoch || 0;
    const result = await api(base + '/channel-connect-flows/' + encodeURIComponent(current.flowId));
    if (panel === current && epoch === panelEpoch && writeEpoch === (current.writeEpoch || 0)) {
      renderChannel(result);
      const flow = result.flow || result;
      if (
        activeRoom() &&
        flow.stage === 'confirm_account' &&
        flow.qr?.scanner_id &&
        !flow.qr.re_pair_required &&
        !flow.qr.account_conflict
      ) {
        const response = await mutation(
          'POST',
          base + '/channel-connect-flows/' + encodeURIComponent(flow.id) + '/actions',
          {
            ...flow.next_request.body,
            confirm_scanner_id: flow.qr.scanner_id,
            idempotency_key: flow.id + ':accept:' + flow.version,
          },
          current,
        );
        if (response && current === panel) renderChannel(response);
        return;
      }
      if (flow.stage === 'ready' && current.lastReadyVersion !== flow.version) {
        current.lastReadyVersion = flow.version;
        await store.refresh();
      }
      if (
        activeRoom() &&
        flow.primary_action === 'poll_qr' &&
        !(flow.next_request?.form_fields || []).length
      ) {
        const response = await mutation(
          'POST',
          base + '/channel-connect-flows/' + encodeURIComponent(flow.id) + '/actions',
          {
            ...flow.next_request.body,
            idempotency_key: flow.id + ':poll:' + flow.version,
          },
          current,
          { transient: true },
        ).catch(() => {
          // A failed background poll is retried by the next tick; it is not a
          // pending user action and must not block the wizard.
          return null;
        });
        if (response && current === panel) renderChannel(response);
      }
    }
  }
  async function recoverSend(id) {
    const receipt = state.pending.get(id);
    if (!receipt) return;
    const owner = state.owner;
    if (receipt.state === 'failed') {
      await store.send(receipt);
      return;
    }
    const result = await api(
      store.path(receipt.roomId) +
        '/messages?client_message_id=' +
        encodeURIComponent(receipt.body.work ? 'work-input:' + id : id),
    );
    const message =
      result.message ||
      result.messages?.find(
        (item) => item.client_message_id === (receipt.body.work ? 'work-input:' + id : id),
      );
    if (owner !== state.owner) return;
    if (message) {
      store.completeReceipt(receipt, { ...result, message });
    } else if (result.not_saved === true && store.writable(receipt.roomId))
      await store.send(receipt);
    else if (result.not_saved === true)
      announce('原消息尚未保存。房间当前只读，原草稿与请求已保留，没有重新发送。');
    else announce('原消息仍在核对，恢复连接后继续查看。');
  }
  async function click(button) {
    const action = button.dataset.crAction,
      id = button.dataset.id;
    if (
      [
        'reply',
        'attach',
        'retry-file',
        'supplement',
        'correct-context',
        'add-member',
        'lead',
        'channel-existing',
      ].includes(action) &&
      !activeRoom()
    ) {
      panel ? panelError(readOnlyReason()) : announce(readOnlyReason(), true);
      return;
    }
    if (action === 'create') createPanel();
    else if (action === 'more-rooms') await store.list({ more: true });
    else if (action === 'archives') await store.list({ includeArchived: !state.includeArchived });
    else if (action === 'select') await selectRoom(id);
    else if (action === 'rooms') {
      const visible = root.classList.toggle('cr-show-rooms');
      button.setAttribute('aria-expanded', String(visible));
    } else if (action === 'open-member') await memberPanel(id);
    else if (action === 'migration') migrationPanel();
    else if (action === 'refresh-migration') await store.refresh();
    else if (action === 'legacy-channel-pause' || action === 'inline-channel-pause') {
      const blocker = state.room?.migration?.blockers.find((item) => item.binding_id === id);
      const remediation = blocker && legacyChannelRemediation(blocker);
      if (!remediation) {
        panelError('原连接已变化，请重新核对迁移条件。');
        return;
      }
      openPanel(
        'legacy-channel-pause',
        blocker.channel === 'weixin' ? '暂停原微信连接' : '暂停原 Telegram 连接',
        `<p>暂停此房间的原渠道连接，停止接收新的渠道输入。账号、历史与已记录的投递回执保留，不会创建新连接、重新批准工作或自动重发旧消息。</p><p class="cr-muted">原连接 ${E(identifier(blocker.binding_id))} · 版本 ${E(blocker.binding_version)}</p>${btn('confirm-legacy-channel-pause', '确认暂停原连接', '', 'cr-danger cr-full')}${btn('migration', '重新核对迁移条件', '', 'cr-text-button')}`,
        { remediation },
      );
    } else if (action === 'confirm-legacy-channel-pause') {
      const remediation = panel.remediation;
      const result = await mutation(remediation.method, remediation.path, remediation.body);
      if (result) {
        await store.refresh();
        migrationPanel();
      }
    } else if (action === 'projects' || action === 'permissions' || action === 'member-scope')
      await permissionsPanel();
    else if (action === 'reload-permissions') await permissionsPanel(panel?.returnChannel);
    else if (action === 'refresh-continuation-preview') {
      const form = button.closest('[data-cr-form="permissions"]');
      if (form && activeRoom()) await previewContinuation(form, panel);
    } else if (action === 'work-list') {
      openPanel('work-list', '房间工作', '<div data-cr-work-list-view></div>');
      renderWorkList();
    } else if (action === 'members') membersPanel();
    else if (action === 'add-member') await addMemberPanel();
    else if (action === 'more-connections') await loadConnections(!!panel.connectionsCursor);
    else if (action === 'member-detail') await memberPanel(id);
    else if (action === 'channel-project-permissions') {
      if (panel?.name === 'weixin') await permissionsPanel(panel.channel);
    } else if (action === 'channel-scope-back') await returnToChannel(panel);
    else if (action === 'close') closePanel();
    else if (action === 'settings') settingsPanel();
    else if (action === 'weixin') await weixinPanel();
    else if (action === 'channel-start') await startWeixin(panel);
    else if (action === 'copy-pairing') {
      const code = panel?.channel?.binding?.pairing_challenge;
      if (!code) return;
      try {
        await navigator.clipboard.writeText(code);
        button.textContent = '已复制';
        setTimeout(() => {
          if (button.isConnected) button.textContent = '复制';
        }, 1600);
      } catch {
        const node = root.querySelector('.cr-pairing-code');
        if (node) window.getSelection()?.selectAllChildren(node);
        panelError('浏览器不允许写入剪贴板，已选中配对码，请按 ⌘C / Ctrl+C 复制。');
      }
    } else if (action === 'channel-existing') {
      const account = root.querySelector('[name="existing_account_id"]')?.value;
      if (!account) {
        panelError('请选择微信账号。');
        return;
      }
      const result = await mutation(
        'POST',
        base + '/channel-connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: 'select_account', account_id: account, expected_version: panel.channel.version },
      );
      if (result) renderChannel(result);
    } else if (action === 'channel-action') {
      const result = await mutation(
        'POST',
        base + '/channel-connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: button.dataset.channelAction, expected_version: panel.channel.version },
      );
      if (result) {
        renderChannel(result);
        await store.refresh();
      }
    } else if (action === 'retry-delivery') {
      const result = await mutation(
        'POST',
        store.path() + '/weixin-deliveries/' + encodeURIComponent(id) + '/retry',
        { expected_version: Number(button.dataset.version) },
      );
      if (result) {
        await store.messages();
        announce('已重新排队发送');
      }
    } else if (action === 'copy-instructions') {
      await navigator.clipboard.writeText(panel.joinInstructions);
      button.textContent = '已复制';
    } else if (action === 'reply') {
      store.draft().reply = id;
      patchComposer();
      root.querySelector('#cr-message').focus();
    } else if (action === 'cancel-reply') {
      store.draft().reply = '';
      patchComposer();
    } else if (action === 'cancel-supplement') {
      store.draft().work = '';
      store.draft().workVersion = 0;
      patchComposer();
    } else if (action === 'attach') root.querySelector('#cr-file').click();
    else if (action === 'remove-file') store.removeFile(id);
    else if (action === 'retry-file') {
      const entry = store.draft().files.find((file) => file.id === id);
      if (entry) await store.upload(entry);
    } else if (action === 'older') {
      const reading = readingSnapshot();
      rememberReading();
      root._loadingOlder = true;
      try {
        await store.messages(true);
        restoreAnchor(reading);
      } finally {
        if (root) {
          root._loadingOlder = false;
          rememberReading();
        }
      }
    } else if (action === 'latest') {
      const view = root,
        epoch = state.epoch,
        hadFocus = document.activeElement === button;
      const current = () => root === view && state.epoch === epoch;
      store.draft().reading = {
        atEnd: true,
        anchorId: '',
        workId: '',
        visibilityRevision: state.visibilityRevision,
      };
      view._restoringReading = true;
      try {
        if (state.messageHistory || state.newerMessages) await store.latestMessages();
      } catch (error) {
        if (current()) throw error;
      } finally {
        if (current()) view._restoringReading = false;
      }
      if (!current()) return;
      const feed = view.querySelector('.cr-feed');
      if (
        hadFocus &&
        (document.activeElement === button || document.activeElement === document.body)
      )
        feed.focus({ preventScroll: true });
      feed.scrollTop = feed.scrollHeight;
      view.querySelector('[data-cr-latest]').hidden = true;
      rememberReading();
    } else if (action === 'recover-send') await recoverSend(id);
    else if (action === 'recover-action') await recoverAction(id);
    else if (action === 'connection-renewal') {
      const current = panel;
      const keepConnected = button.dataset.renewalEnable === 'true';
      const flow = current.flow;
      if (
        keepConnected &&
        !(await uiConfirmAction(
          {
            title: '在房间授权有效期间保持连接？',
            body: 'CodePier 会为此房间的当前 dot 连接定期续期。可以在连接详情关闭。',
            confirmLabel: '开启托管续期',
          },
          {
            owner: button,
            current: () => panel === current && current.flow.version === flow.version,
          },
        ))
      )
        return;
      const result = await mutation(
        'POST',
        base + '/connect-flows/' + encodeURIComponent(current.flowId) + '/actions',
        { action: 'set_renewal', expected_version: flow.version, keep_connected: keepConnected },
        current,
      );
      if (result) {
        renderFlow(result);
        await store.refresh();
      }
    } else if (action === 'flow-action') {
      const flow = panel.flow,
        requestAction = button.dataset.flowAction;
      if (requestAction === 'view_details') {
        root.querySelector('.cr-diagnostics').open = true;
        return;
      }
      const mapped =
        {
          continue_connection: 'resume',
          recover_connection: 'resume',
          retry_probe: 'resume',
          refresh_invitation: 'refresh_invite',
        }[requestAction] || requestAction;
      const result = await mutation(
        'POST',
        base + '/connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: mapped, expected_version: flow.version },
      );
      if (result) {
        renderFlow(result);
        if (requestAction === 'retry_probe') announce('连接检查已排队，等待 dot 回复');
        await store.refresh();
      }
    } else if (action === 'rebind-member') {
      if (panel.flow.pending_binding) {
        root.querySelector('.cr-instructions')?.focus();
        panelError('请继续本页的更换连接说明。验证成功前，当前连接仍然保留。');
        return;
      }
      const current = panel;
      openPanel(
        'rebind-member',
        '更换 dot 连接',
        `<p>为 ${E(current.member.name || '此成员')} 生成新的接入流程。新宿主完成原生订阅和往返验证后，才会替换当前连接；成员与原工作仍保留。</p>${btn('confirm-rebind', '开始更换连接', '', 'cr-primary cr-full')}`,
        { member: current.member, flow: current.flow, flowId: current.flowId },
      );
    } else if (action === 'confirm-rebind') {
      const result = await mutation(
        'POST',
        base + '/connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: 'rebind', expected_version: panel.flow.version },
      );
      if (result) {
        openPanel('member', result.member?.name || 'dot 连接', '<div data-cr-flow-view></div>', {
          member: result.member,
          flowId: result.flow.id,
        });
        renderFlow(result);
        await store.refresh();
      }
    } else if (action === 'remove-member') {
      const current = panel;
      openPanel(
        'remove-member',
        '移出成员',
        `<p>成员将不再接收新消息或开始新步骤；已有操作仍保留原回执。</p>${btn('confirm-remove-member', '移除成员', '', 'cr-danger cr-full')}`,
        { member: current.member, flow: current.flow, flowId: current.flowId },
      );
    } else if (action === 'confirm-remove-member') {
      const current = panel;
      const result = await mutation(
        'POST',
        base + '/connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: 'remove', expected_version: panel.flow.version },
      );
      if (result) {
        await store.refresh();
        if (panel === current && root?.querySelector('dialog')?.open) membersPanel();
      }
    } else if (action === 'lead') {
      const current = panel;
      const result = await mutation('PATCH', store.path(), {
        lead_member_id: id,
        expected_version: state.room.version,
      });
      if (result) {
        await store.refresh();
        if (panel === current && root?.querySelector('dialog')?.open) membersPanel();
      }
    } else if (action === 'room-state') {
      const result = await mutation('PATCH', store.path(), {
        state: button.dataset.state,
        expected_version: state.room.version,
      });
      if (result) {
        closePanel();
        await store.refresh();
      }
    } else if (action === 'archive-room') {
      if (state.room.state === 'archived') {
        const result = await mutation('PATCH', store.path(), {
          state: 'active',
          expected_version: state.room.version,
        });
        if (result) {
          closePanel();
          await store.refresh();
        }
      } else
        openPanel(
          'archive',
          '归档房间',
          '<p>归档会停止工作并撤销当前连接、授权和订阅，历史记录会保留。恢复只恢复历史和项目关联，不会自动恢复执行权限，需重新确认接入。</p>' +
            btn('room-state', '归档房间', 'data-state="archived"', 'cr-danger cr-full'),
        );
    } else if (action === 'work-detail') await workPanel(id);
    else if (action === 'more-work') {
      rememberReading();
      button.disabled = true;
      try {
        await store.works(!!state.worksCursor);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    } else if (action === 'latest-work') {
      await store.latestWorks();
      root.querySelector('[data-cr-work]')?.scrollIntoView({ block: 'start', behavior: 'instant' });
      rememberReading();
    } else if (action === 'work-control') await controlWork(button.dataset.workAction);
    else if (action === 'supplement') {
      store.draft().work = panel.work.id;
      store.draft().workVersion = panel.work.version;
      store.draft().contextCorrection = null;
      closePanel({ restoreFocus: false });
      patchComposer();
      root.querySelector('#cr-message').focus();
      announce('下一条消息将补充到这项工作');
    } else if (action === 'diagnostics') {
      openPanel(
        'diagnostics',
        '运行诊断',
        '<div data-cr-diagnostics-view><p class="cr-muted" data-cr-diagnostics-loading role="status">正在读取…</p></div>',
      );
      await loadDiagnostics();
    } else if (action === 'refresh-diagnostics') await loadDiagnostics();
    else if (action === 'context') {
      openPanel(
        'context',
        '房间要点',
        '<div data-cr-context-view><p class="cr-muted">正在核对…</p></div>',
        { context: state.room?.context },
      );
      renderContext();
      await loadContext();
    } else if (action === 'more-context') await loadContext(true);
    else if (action === 'refresh-context') await loadContext();
    else if (action === 'context-source') await contextSource(id, button.dataset.sourceVersion);
    else if (action === 'correct-context') {
      const value = panel?.context;
      if (
        !activeRoom() ||
        value?.writable === false ||
        value?.archived ||
        !value?.facts.some((fact) => fact.id === id)
      )
        return;
      remember();
      const draft = store.draft();
      draft.contextCorrection = {
        ids: [id],
        version: value.version,
        visibilityRevision: value.visibility_revision,
      };
      draft.work = '';
      draft.workVersion = 0;
      closePanel();
      patchComposer();
      root.querySelector('#cr-message').focus();
      announce('请在原输入框写明纠正内容。发送后才会撤下所选旧事实。');
    } else if (action === 'cancel-context-correction') {
      store.draft().contextCorrection = null;
      patchComposer();
    } else if (action === 'locate-message') {
      closePanel();
      const node = [...root.querySelectorAll('[data-message-id]')].find(
        (item) => item.dataset.messageId === id,
      );
      if (node) {
        node.scrollIntoView({ block: 'center', behavior: 'instant' });
        node.tabIndex = -1;
        node.focus({ preventScroll: true });
      }
    }
  }
  function closeHints() {
    hints?.close();
    hints = null;
    hintsConnected = false;
  }
  function wake() {
    if (pollPromise) {
      pollAgain = true;
      return;
    }
    clearTimeout(timer);
    void poll();
  }
  function connectHints() {
    if (
      !root ||
      !state.roomId ||
      hints ||
      document.hidden ||
      accessBlocked ||
      typeof EventSource === 'undefined'
    )
      return;
    const roomId = state.roomId,
      owner = state.owner;
    const source = (hints = new EventSource(
      store.path() + '/events' + (S.space_id ? '?space_id=' + encodeURIComponent(S.space_id) : ''),
    ));
    const current = () =>
      hints === source &&
      !!root &&
      state.roomId === roomId &&
      state.owner === owner &&
      S.page === 'collaboration';
    let revision = '';
    source.onopen = () => {
      if (current()) {
        hintsConnected = true;
        schedule();
      }
    };
    source.onerror = () => {
      if (current()) {
        hintsConnected = false;
        wake();
      }
    };
    source.addEventListener('change', (event) => {
      if (!current()) return;
      let value;
      try {
        value = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!value.revision || value.revision === revision) return;
      revision = value.revision;
      wake();
    });
    source.addEventListener('access_changed', () => {
      if (!current()) return;
      // Clear the old projection before waiting on a new authorization check.
      const invalidatedPanel = panel;
      closeHints();
      store.abort();
      store.clearWindows();
      root._restoringReading = false;
      closePanel();
      // A project edit can invalidate its own room stream before its PUT
      // response arrives. Retain only the identity of that closed submission,
      // never its old projection or permission. A later panel changes the fence.
      if (invalidatedPanel?.name === 'permissions' && invalidatedPanel.busy)
        invalidatedPanel.accessInvalidation = {
          owner,
          roomId,
          epoch: state.epoch,
          openingEpoch: invalidatedPanel.epoch,
        };
      const saved = invalidatedPanel?.savedPermissionFeedback;
      if (
        saved &&
        saved.owner === owner &&
        saved.roomId === roomId &&
        saved.openingEpoch === invalidatedPanel.epoch
      ) {
        saved.epoch = state.epoch;
        saved.accessInvalidated = true;
        root._crProjectSaveFeedback = saved;
        announce(saved.message);
      }
      root.querySelector('.cr-panel-content').replaceChildren();
      const pending = root.querySelector('[data-cr-pending]');
      pending.replaceChildren();
      pending._markup = null;
      state.room = null;
      state.messages = [];
      state.works = [];
      state.worksCursor = null;
      state.worksComplete = true;
      state.worksVisibilityRevision = null;
      state.cursor = null;
      state.visibilityRevision = null;
      state.messageRevision = 0;
      delete store.draft(roomId).reading;
      state.rooms = state.rooms.filter((room) => room.id !== roomId);
      patchHeader();
      patchMessages();
      patchWorks();
      accessBlocked = true;
      Promise.allSettled([store.refresh(), store.list()]).then((results) => {
        if (!root || state.roomId !== roomId || state.owner !== owner) return;
        const failure = results.find((result) => result.status === 'rejected');
        const denied = results.some(
          (result) =>
            result.status === 'rejected' && [401, 403, 404].includes(result.reason?.status),
        );
        if (denied) announce('房间权限已变化，请重新选择可用房间。', true);
        else {
          // A successful write may supersede this read. Abort is a transport
          // fence, never evidence that the owner lost room access.
          accessBlocked = false;
          patchHeader();
          patchPending();
          connectHints();
          if (failure) wake();
          else schedule();
        }
      });
    });
  }
  async function poll() {
    if (!root || document.hidden || accessBlocked || S.page !== 'collaboration') return schedule();
    if (pollPromise) return pollPromise;
    const epoch = state.epoch,
      savedFeedback = root._crProjectSaveFeedback;
    pollPromise = (async () => {
      try {
        await store.refresh();
        if (epoch !== state.epoch) return;
        if (root && root._crProjectSaveFeedback === savedFeedback)
          delete root._crProjectSaveFeedback;
        if (panel?.name === 'member') await refreshFlow();
        if (panel?.name === 'weixin') await refreshChannel();
        if (panel?.name === 'work' && !panel.busy) await store.work(panel.work.id);
        if (panel?.name === 'permissions') void refreshContinuationResult(panel);
      } catch (error) {
        if (error.name !== 'AbortError' && root && epoch === state.epoch) {
          const saved = root._crProjectSaveFeedback;
          const confirmed =
            saved &&
            saved.owner === state.owner &&
            saved.roomId === state.roomId &&
            saved.epoch === state.epoch &&
            !panel &&
            saved.openingEpoch === root.querySelector('dialog')?._crOpeningEpoch;
          announce(
            (confirmed ? saved.message + ' 视图刷新失败：' : '刷新暂时失败，草稿与原工作已保留。') +
              error.message,
            true,
          );
        }
      }
    })().finally(() => {
      pollPromise = null;
      if (pollAgain && root && !document.hidden && !accessBlocked) {
        pollAgain = false;
        clearTimeout(timer);
        timer = setTimeout(poll, 0);
      } else schedule();
    });
    return pollPromise;
  }
  function schedule() {
    clearTimeout(timer);
    if (root && !accessBlocked)
      timer = setTimeout(poll, panel?.flowId ? 1500 : hintsConnected ? 15000 : 5000);
  }
  function bind() {
    root = document.querySelector('.cr-shell');
    if (!root || bound === root) return;
    bound = root;
    abortEvents?.abort();
    abortEvents = new AbortController();
    const signal = abortEvents.signal;
    unsubscribe?.();
    unsubscribe = store.on(update);
    const editor = root.querySelector('#cr-message');
    root._restoringReading = !!store.draft().reading;
    editor.value = store.draft().text;
    sizeEditor(editor);
    patchHeader();
    patchMessages();
    patchWorks();
    patchPending();
    void restoreReading().catch((error) => {
      if (error.name !== 'AbortError') announce(error.message, true);
    });
    root.querySelector('[data-cr-older]').hidden = !state.cursor;
    accessBlocked = false;
    connectHints();
    if (state.loadError) {
      announce(state.loadError, true);
      delete state.loadError;
    }
    root.addEventListener(
      'click',
      (event) => {
        const button = event.target.closest('[data-cr-action]');
        if (!button || !root.contains(button) || button.disabled) return;
        if (!button.closest('dialog')) root._panelOpener = button;
        event.preventDefault();
        click(button).catch((error) => {
          if (error.name !== 'AbortError')
            panel ? panelError(error.message) : announce(error.message, true);
        });
      },
      { signal },
    );
    root.addEventListener(
      'submit',
      (event) => {
        event.preventDefault();
        if (event.target.id === 'cr-composer') {
          remember();
          const draft = store.draft(),
            roomId = state.roomId,
            owner = state.owner;
          if (
            (!draft.text.trim() && !draft.files.length) ||
            !activeRoom() ||
            state.room.state === 'paused' ||
            contextCorrectionStale() ||
            draft.files.some((entry) => entry.state !== 'ready')
          )
            return;
          if (
            [...state.pending.values()].some(
              (item) => item.roomId === state.roomId && store.sameDraft(item),
            )
          )
            return;
          store.send().catch(async (error) => {
            if (state.roomId !== roomId || state.owner !== owner) return;
            if (error.code === 'CONTEXT_VERSION_CHANGED') await store.refresh().catch(() => {});
            if (state.roomId === roomId && state.owner === owner) announce(error.message, true);
          });
        } else if (event.target.matches('[data-cr-form]')) {
          const current = panel,
            owner = state.owner,
            epoch = state.epoch;
          if (current && !current.busy) panelError('');
          submitForm(event.target).catch((error) => {
            if (
              error.name === 'AbortError' ||
              current !== panel ||
              owner !== state.owner ||
              epoch !== state.epoch
            )
              return;
            // mutation() owns HTTP/CAS messages; also surface failures thrown
            // before it runs, while preserving this submission's precise hint.
            const existing = root?.querySelector('[data-cr-panel-error]')?.textContent;
            panelError(existing || error.message || '未能完成提交，请核对当前选择。');
          });
        }
      },
      { signal },
    );
    editor.addEventListener(
      'input',
      () => {
        remember();
        sizeEditor(editor);
        patchComposer();
      },
      { signal },
    );
    root.querySelector('[data-cr-room-filter]')?.addEventListener(
      'input',
      (event) => {
        roomFilter = event.target.value;
        patchRooms();
      },
      { signal },
    );
    editor.addEventListener(
      'compositionstart',
      () => {
        editor._composing = true;
      },
      { signal },
    );
    editor.addEventListener(
      'compositionend',
      () => {
        editor._composing = false;
      },
      { signal },
    );
    editor.addEventListener(
      'keydown',
      (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !editor._composing) {
          event.preventDefault();
          root.querySelector('#cr-composer').requestSubmit();
        }
      },
      { signal },
    );
    root.addEventListener(
      'change',
      (event) => {
        if (
          event.target.name === 'target_member_id' &&
          event.target.form?.dataset.crForm === 'work-recovery'
        )
          updateWorkRecoveryScope(event.target.form);
        if (event.target.id === 'cr-file') {
          const selected = [...event.target.files];
          event.target.value = '';
          try {
            for (const entry of store.addFiles(selected)) {
              store.upload(entry).catch((error) => {
                if (entry.owner === state.owner && entry.roomId === state.roomId)
                  announce(error.message, true);
              });
            }
          } catch (error) {
            announce(error.message, true);
          }
        }
        if (event.target.name === 'connection_mode' && panel?.name === 'add-member') {
          panel.modeTouched = true;
          const existing = event.target.value === 'existing',
            select = root.querySelector('[name="reuse_member_id"]');
          root.querySelector('[data-cr-connections]').hidden = !existing;
          select.disabled = !existing;
          select.required = existing;
          if (existing && !panel.connectionsLoaded) void loadConnections();
        }
        if (event.target.name === 'reuse_member_id' && panel?.name === 'add-member') {
          const source = panel.connections.find(
            (item) => (item.member_id || item.id) === event.target.value,
          );
          const name = root.querySelector('[data-cr-form="member"] [name="name"]');
          if (source && (!name.value.trim() || name.value === panel.lastSourceName))
            name.value = source.name;
          panel.lastSourceName = source?.name || '';
        }
        if (event.target.name === 'member_id' && event.target.closest('#cr-composer'))
          store.draft().member = event.target.value;
        if (['allow_owner_tasks', 'confirm_follow_room'].includes(event.target.name))
          updateChannelDependencies();
        if (event.target.matches('[data-permission-level]')) {
          applyPermissionLevel(event.target, event.target.value);
          updatePermissionControls();
        }
        if (event.target.matches('[data-permission-project], [data-permission-select-all]')) {
          const { target } = event;
          const section = target.closest('[data-permission-section]');
          const projects = target.matches('[data-permission-project]')
            ? [target]
            : section.querySelectorAll('[data-permission-project]:not(:disabled)');
          for (const project of projects) {
            const select = project
              .closest('[data-permission-row]')
              .querySelector('[data-permission-level]');
            if (!target.checked || !select.value)
              applyPermissionLevel(select, target.checked ? 'read' : '');
          }
          updatePermissionControls();
        }
        if (event.target.matches('[data-permission-bulk-level]')) {
          const section = event.target.closest('[data-permission-section]');
          const requested = permissionLevels[event.target.value]?.[1];
          if (!requested) return;
          for (const project of section.querySelectorAll(
            '[data-permission-project]:checked:not(:disabled)',
          )) {
            const select = project
              .closest('[data-permission-row]')
              .querySelector('[data-permission-level]');
            const available = [...select.options].filter((option) => !option.disabled);
            const best = available
              .reverse()
              .find((option) =>
                permissionLevels[option.value][1].every((cap) => requested.includes(cap)),
              );
            if (best) applyPermissionLevel(select, best.value, true);
          }
          event.target.value = '-';
          updatePermissionControls();
        }
        if (
          event.target.matches(
            '[data-permission-global-expiry], [data-permission-global-date] input',
          )
        ) {
          applyGlobalPermissionExpiry(event.target.closest('[data-permission-section]'));
          updatePermissionControls();
        }
        if (event.target.matches('[data-permission-target-option]')) updatePermissionControls();
        if (
          event.target.matches('[data-permission-lifetime-mode],[data-permission-lifetime-until]')
        ) {
          event.target.closest('[data-permission-lifetime]').dataset.lifetimeEdited = 'true';
          updatePermissionControls();
        }
        if (event.target.matches('[data-permission-mode]')) updatePermissionControls();
        const form = event.target.closest('[data-cr-form="permissions"]');
        if (!form || !activeRoom() || panel?.name !== 'permissions') return;
        if (event.target.matches('[data-cr-continuation-all]')) {
          for (const task of form.querySelectorAll('[data-cr-continuation-task]:not(:disabled)'))
            task.checked = event.target.checked;
          updateContinuationSelection(form);
        } else if (event.target.matches('[data-cr-continuation-task]')) {
          updateContinuationSelection(form);
        } else if (
          event.target.matches('[data-cr-continue-selected]') ||
          (form.querySelector('[data-cr-continue-selected]').checked &&
            event.target.closest('[data-permission-section], [data-permission-mode]'))
        ) {
          void previewContinuation(form, panel);
        }
      },
      { signal },
    );
    root.querySelector('dialog').addEventListener(
      'close',
      () => {
        const dialog = root?.querySelector('dialog');
        // A queued close event can belong to an earlier opening of this same
        // dialog. Do not tear down a panel that has already been reopened.
        if (dialog?.open) return;
        panelEpoch++;
        if (panel?.name === 'work') state.workDetail = null;
        panel = null;
        const opener = dialog?._crOpener;
        if (!opener?.disabled) CP.ui.restoreLayerFocus(opener);
      },
      { signal },
    );
    root.querySelector('.cr-feed').addEventListener(
      'scroll',
      () => {
        const feed = root.querySelector('.cr-feed');
        rememberReading();
        if (!state.newerMessages && feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90)
          root.querySelector('[data-cr-latest]').hidden = true;
      },
      { signal, passive: true },
    );
    document.addEventListener(
      'visibilitychange',
      () => {
        if (document.hidden) closeHints();
        else {
          connectHints();
          wake();
        }
      },
      { signal },
    );
    schedule();
  }
  async function open(project) {
    requestedProject = project || '';
    const matching = state.rooms.find((room) =>
      (room.projects || []).some((p) => p.project_id === project),
    );
    if (matching) selection(matching.id);
    if (S.page === 'collaboration' && root) {
      if (matching) return selectRoom(matching.id);
      createPanel();
      return;
    }
    return navigate('collaboration');
  }
  window.addEventListener('pagehide', () => {
    if (typeof S !== 'undefined' && S.page === 'collaboration') {
      suspended = true;
      detach();
    }
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && suspended) {
      suspended = false;
      if (S.page === 'collaboration' && S.session)
        renderPage(false).catch((error) => toast(error.message, true));
    }
  });
  return {
    html,
    bind,
    detach,
    clear,
    open,
    presentation: { inboundHealthMarkup, uploadEstimateMarkup },
  };
})();
