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
    queued: '已排队',
    running: '正在处理',
    waiting_member: '等待 dot',
    waiting_input: '等待补充',
    waiting_user: '等待你补充',
    waiting_authorization: '等待权限',
    waiting_target: '等待设备',
    verifying: '正在核验',
    succeeded: '已完成',
    completed: '已完成',
    paused: '已暂停',
    cancelling: '正在停止',
    cancelled: '已停止',
    stopping: '正在停止',
    reconciling: '正在核对原操作',
    blocked: '等待继续',
    failed: '处理失败',
    unknown: '正在核对原操作',
    leased: '已领取',
    reconnecting: '等待执行目标恢复',
    expired: '已过期',
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
  const activeRoom = () => state.room && state.room.state !== 'archived';
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
  const actionLabel = (action) =>
    ({
      refresh_invite: '刷新加入说明',
      resume: '继续连接',
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
      approve_tasks: '确认工作范围',
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
    }
  }
  function remember() {
    const editor = root?.querySelector('#cr-message');
    if (editor && state.roomId) store.draft().text = editor.value;
  }
  function rememberReading() {
    const feed = root?.querySelector('.cr-feed');
    if (!feed || !state.roomId || !state.room || root._restoringReading) return;
    const edge = feed.getBoundingClientRect().top;
    const anchor = [...root.querySelectorAll('[data-message-id]')].find(
      (node) => node.getBoundingClientRect().bottom > edge + 8,
    );
    store.draft().reading = {
      anchorId: anchor?.dataset.messageId || '',
      offset: anchor ? anchor.getBoundingClientRect().top - edge : 0,
      atEnd: feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90,
      visibilityRevision: state.visibilityRevision,
    };
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
      [...view.querySelectorAll('[data-message-id]')].find(
        (node) => node.dataset.messageId === reading.anchorId,
      );
    try {
      // Fetch only while the remembered anchor is outside the initial page.
      // Bound automatic restoration; older history remains explicitly paged.
      for (
        let pages = 0;
        current() &&
        store.draft().reading === reading &&
        !reading.atEnd &&
        !findAnchor() &&
        state.cursor &&
        pages < 20;
        pages++
      )
        await store.messages(true);
      if (!current() || store.draft().reading !== reading) return;
      const feed = view.querySelector('.cr-feed'),
        anchor = findAnchor();
      if (reading.atEnd) feed.scrollTop = feed.scrollHeight;
      else if (anchor)
        feed.scrollTop +=
          anchor.getBoundingClientRect().top - feed.getBoundingClientRect().top - reading.offset;
      else {
        delete store.draft().reading;
        announce('原阅读位置已不可见，已保留当前可读取的历史。');
      }
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
      <aside class="cr-sidebar" aria-label="房间列表"><header><div><span class="cr-eyebrow">WORKSPACE</span><h2>协作中心</h2></div>${btn('create', icon('plus'), 'aria-label="新建房间"', 'cr-icon cr-primary')}</header><div class="cr-room-list" data-cr-rooms></div>${btn('more-rooms', '更多房间', 'data-cr-more-rooms hidden', 'cr-text-button')}<footer>${btn('archives', '显示已归档', 'aria-pressed="false"', 'cr-text-button')}<span class="cr-status-dot" aria-hidden="true"></span></footer></aside>
      <main class="cr-main"><header class="cr-topbar"><div class="cr-room-heading">${btn('rooms', icon('menu'), 'aria-label="显示房间列表" aria-expanded="false"', 'cr-icon cr-mobile')}<div><h1 tabindex="-1" data-cr-title></h1><span class="cr-subtitle" data-cr-status></span></div></div><nav class="cr-room-actions" aria-label="房间管理">${btn('projects', icon('folder') + '<span data-cr-project-count>项目</span>')}${btn('members', '<span class="cr-avatars" data-cr-avatars></span><span>成员</span>')}${btn('weixin', icon('message') + '<span data-cr-weixin-label>连接微信</span>')}${btn('settings', icon('settings'), 'aria-label="房间设置"', 'cr-icon')}</nav></header>
        <div class="cr-legacy-note" data-cr-legacy hidden>原房间的历史和工作已保留。成员连接需要逐一完成迁移。${btn('members', '查看成员')}</div>
        <div class="cr-feedback" role="status" aria-live="polite" data-cr-feedback></div>
        <section class="cr-feed" aria-label="房间对话" tabindex="0"><div class="cr-load-older" data-cr-older hidden>${btn('older', '更早的消息')}</div><div data-cr-empty></div><div class="cr-messages" role="log" aria-live="polite" aria-relevant="additions text" data-cr-messages></div><div class="cr-pending" data-cr-pending></div><div class="cr-work-list" data-cr-work></div></section>
        ${btn('latest', '查看新消息 ↓', 'data-cr-latest hidden', 'cr-latest')}
        <form class="cr-composer" id="cr-composer"><div class="cr-reply" data-cr-reply hidden></div><div class="cr-draft-files" data-cr-files aria-label="待发送的附件" aria-live="polite" hidden></div><div class="cr-composer-line"><textarea id="cr-message" name="body_text" aria-label="消息" placeholder="说说你想做什么…" rows="1" maxlength="8000"></textarea><button type="submit" class="cr-send" aria-label="发送消息">${icon('arrow')}</button></div><footer><div class="cr-composer-tools">${btn('attach', icon('plus'), 'aria-label="添加附件" title="每个附件最多 8 MiB"', 'cr-icon cr-attach')}<input type="file" id="cr-file" multiple hidden aria-label="选择附件"><label class="cr-recipient"><span>发送给</span><select name="member_id" aria-label="选择处理成员"><option value="">主责 dot</option></select></label></div><span data-cr-compose-status>Enter 发送 · Shift + Enter 换行</span></footer></form>
      </main><dialog class="cr-panel" aria-labelledby="cr-panel-title"><header><h2 id="cr-panel-title"></h2>${btn('close', '×', 'aria-label="关闭面板"', 'cr-icon')}</header><div class="cr-panel-content"></div><p role="alert" class="cr-error" data-cr-panel-error hidden></p><div class="cr-mutation-recovery" data-cr-panel-recovery hidden></div></dialog>
    </section>`;
  }
  function patchRooms() {
    const list = root?.querySelector('[data-cr-rooms]');
    if (!list) return;
    const markup = state.rooms.length
      ? state.rooms
          .map(
            (room) =>
              `<button type="button" class="cr-room ${room.id === state.roomId ? 'is-current' : ''}" data-cr-action="select" data-id="${E(room.id)}" aria-current="${room.id === state.roomId ? 'true' : 'false'}"><span class="cr-room-symbol" aria-hidden="true">${icon('message')}</span><span><strong>${E(room.title || '未命名房间')}</strong><small>${E(room.display?.subtitle || (room.state === 'archived' ? '已归档' : room.state === 'paused' ? '已暂停' : (room.projects?.length || 0) + ' 个项目'))}</small></span><span class="cr-room-chevron" aria-hidden="true">›</span></button>`,
          )
          .join('')
      : '<p class="cr-list-empty">还没有房间</p>';
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
    root.querySelector('[data-cr-title]').textContent = room?.title || '开始一段协作';
    root.querySelector('[data-cr-status]').textContent =
      room?.display?.subtitle ||
      room?.display?.title ||
      (room?.state === 'paused'
        ? '已暂停'
        : room?.state === 'archived'
          ? '已归档'
          : room
            ? '待命'
            : '把项目和 dot 放进同一个房间');
    root.querySelector('[data-cr-project-count]').textContent =
      '项目 ' + (room?.projects?.filter((p) => p.state !== 'removed').length || 0);
    root.querySelector('[data-cr-legacy]').hidden = !room?.legacy;
    const avatars = root.querySelector('[data-cr-avatars]');
    const avatarMarkup = members()
      .slice(0, 3)
      .map((m) => `<i aria-hidden="true">${E((m.name || m.label || 'D').slice(0, 1))}</i>`)
      .join('');
    if (avatars.innerHTML !== avatarMarkup) avatars.innerHTML = avatarMarkup;
    const label = room?.channels?.find((channel) => channel.kind === 'weixin');
    root.querySelector('[data-cr-weixin-label]').textContent =
      label?.display?.title || (label?.state === 'ready' ? '微信已连接' : '连接微信');
    for (const node of root.querySelectorAll('.cr-room-actions button')) node.disabled = !room;
    patchRooms();
    patchComposer();
    patchEmpty();
  }
  function patchEmpty() {
    const node = root?.querySelector('[data-cr-empty]');
    if (!node) return;
    const show = !state.messages.length && !state.works.length;
    node.hidden = !show;
    const markup = !state.room
      ? `<div class="cr-empty"><div class="cr-orbit" aria-hidden="true"><span></span><i>${icon('message')}</i><span></span></div><h3>事情，从这里开始</h3><p>一个房间，一段持续的协作。</p>${btn('create', '新建房间', '', 'cr-primary')}</div>`
      : `<div class="cr-empty"><div class="cr-orbit" aria-hidden="true"><span></span><i>${icon('network')}</i><span></span></div><h3>${members().length ? '想一起做点什么？' : '让你的 dot 加入'}</h3><p>${members().length ? '直接发消息，工作和进展都会留在这里。' : '先连接成员，也可以先把目标写下来。'}</p><div class="cr-empty-actions">${!members().length ? btn('add-member', '添加 dot', '', 'cr-primary') : ''}${!(state.room.projects || []).length ? btn('projects', '添加项目') : ''}</div></div>`;
    if (node._markup !== markup) {
      node.innerHTML = markup;
      node._markup = markup;
    }
  }
  function patchComposer() {
    const editor = root?.querySelector('#cr-message'),
      select = root?.querySelector('[name="member_id"]');
    if (!editor) return;
    const draft = store.draft();
    const disabled = !activeRoom() || state.room?.state === 'paused';
    editor.disabled = disabled;
    const unfinished = draft.files.some((entry) => entry.state !== 'ready');
    const duplicate = [...state.pending.values()].some(
      (receipt) => receipt.roomId === state.roomId && store.sameDraft(receipt),
    );
    root.querySelector('.cr-send').disabled =
      disabled || unfinished || duplicate || (!editor.value.trim() && !draft.files.length);
    root.querySelector('[data-cr-action="attach"]').disabled = disabled || draft.files.length >= 16;
    root.querySelector('#cr-file').disabled = disabled;
    const status = root.querySelector('[data-cr-compose-status]');
    status.textContent = unfinished
      ? '请完成附件上传后发送'
      : draft.work
        ? '补充到当前工作'
        : 'Enter 发送 · Shift + Enter 换行';
    patchFiles();
    if (select && !select.matches(':focus')) {
      const markup =
        '<option value="">主责 dot</option>' +
        members()
          .map(
            (m) =>
              `<option value="${E(memberId(m))}">${E(m.name || m.label)} · ${E(memberShort(m))} · ${E(title(m))}</option>`,
          )
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
          `<article class="cr-file" data-file-id="${E(entry.id)}" data-state="${E(entry.state)}"><div><strong>${E(entry.attachment?.name || entry.name)}</strong><small>${fileSize(entry.size)} · ${entry.state === 'ready' ? '已上传，待发送' : entry.state === 'unknown' ? '上传结果待核对' : entry.state === 'failed' ? E(entry.error || '上传失败') : '正在上传…'}</small></div>${['failed', 'unknown'].includes(entry.state) ? btn('retry-file', entry.state === 'unknown' ? '核对原上传' : '重试', `data-id="${E(entry.id)}"`, 'cr-text-button') : ''}${btn('remove-file', '×', `data-id="${E(entry.id)}" aria-label="移除附件 ${E(entry.name)}"`, 'cr-icon')}</article>`,
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
  function messageMarkup(message) {
    const owner = ['owner', 'user', 'human'].includes(message.author_kind);
    return `<header><strong>${E(message.author_name || message.display_name || message.member_name || (owner ? '你' : message.author_kind === 'system' ? '房间动态' : 'dot'))}</strong><time>${E(time(message.created_at || message.created))}</time></header>${message.reply_to_id ? '<span class="cr-message-reference">回复原消息</span>' : ''}<div class="cr-message-body" ${message.body_text ? '' : 'hidden'}></div>${message.attachments?.length ? `<div class="cr-message-files">${message.attachments.map(attachmentMarkup).join('')}</div>` : ''}<footer>${message.display?.title ? `<span>${E(message.display.title)}</span>` : ''}${btn('reply', '回复', `data-id="${E(message.id)}"`, 'cr-text-button')}</footer>`;
  }
  function patchMessages() {
    if (!root) return;
    const list = root.querySelector('[data-cr-messages]'),
      feed = root.querySelector('.cr-feed');
    const nearEnd = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
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
      node.innerHTML = messageMarkup(message);
      const body = node.querySelector('.cr-message-body');
      if (typeof chatRichMarkdown === 'function') chatRichMarkdown(body, message.body_text || '');
      else body.textContent = message.body_text || '';
      changed = true;
    }
    // Stable nodes keep browser selection and scroll intact across projections.
    state.messages.forEach((message, index) => {
      const node = [...list.children].find((child) => child.dataset.messageId === message.id);
      if (node && list.children[index] !== node)
        list.insertBefore(node, list.children[index] || null);
    });
    patchEmpty();
    if (changed && nearEnd && !root._loadingOlder && !root._restoringReading) {
      const top = feed.scrollTop,
        epoch = state.epoch;
      requestAnimationFrame(() => {
        if (!feed.isConnected || epoch !== state.epoch || root?._restoringReading) return;
        if (Math.abs(feed.scrollTop - top) < 2) feed.scrollTop = feed.scrollHeight;
        else root.querySelector('[data-cr-latest]').hidden = false;
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
            `<article class="cr-pending-item"><p>${E(item.body.text)}${item.body.attachments?.length ? `<small>${E(item.body.attachments.map((file) => file.name).join('、'))}</small>` : ''}</p><div>${item.state === 'sending' ? '正在发送…' : item.state === 'unknown' ? '正在核对是否已保存' : E(item.error || '发送失败')}${item.state !== 'sending' ? btn('recover-send', item.state === 'unknown' ? '核对原消息' : '继续发送', `data-id="${E(item.id)}"`) : ''}</div></article>`,
        )
        .join('') + mutationMarkup();
    if (node._markup !== markup) {
      node.innerHTML = markup;
      node._markup = markup;
    }
    patchMutationRecovery();
  }
  function mutationMarkup() {
    return [...state.mutations.values()]
      .filter((item) => item.roomId === state.roomId || item.kind === 'create')
      .map(
        (item) =>
          `<article class="cr-pending-item"><p>${E(item.label)} · 结果尚未确认</p><div>${item.state === 'recovering' ? '正在核对原请求…' : btn('recover-action', '核对原操作', `data-id="${E(item.id)}"`)}</div></article>`,
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
    const markup = state.works
      .map(
        (work) =>
          `<article class="cr-work" data-work-id="${E(work.id)}"><span class="cr-work-indicator ${E(work.state)}" aria-hidden="true"></span><div><strong>${E(work.title || '当前工作')}</strong><p>${E(work.display?.title || workLabels[work.state] || work.state)}${work.display?.reason ? ' · ' + E(work.display.reason) : ''}</p></div>${btn('work-detail', '查看', `data-id="${E(work.id)}"`, 'cr-text-button')}</article>`,
      )
      .join('');
    if (node._markup !== markup) {
      node.innerHTML = markup;
      node._markup = markup;
    }
    patchEmpty();
  }
  function update(domain) {
    if (!root) return;
    if (['room', 'rooms'].includes(domain)) patchHeader();
    if (domain === 'messages') patchMessages();
    if (domain === 'works') {
      patchWorks();
      if (panel?.name === 'work' && !panel.busy) {
        const latest = state.works.find((work) => work.id === panel.work.id);
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
    if (domain === 'page') root.querySelector('[data-cr-older]').hidden = !state.cursor;
  }
  function sizeEditor(editor) {
    editor.style.height = 'auto';
    editor.style.height = Math.min(editor.scrollHeight, 180) + 'px';
  }
  function closePanel() {
    const dialog = root?.querySelector('dialog');
    if (!dialog) return;
    panelEpoch++;
    panel = null;
    dialog.close();
  }
  function openPanel(name, heading, content, data = {}) {
    panelEpoch++;
    panel = { name, roomId: state.roomId, epoch: panelEpoch, key: store.key(), ...data };
    const dialog = root.querySelector('dialog');
    if (!dialog.open)
      dialog._crOpener = root._panelOpener?.isConnected
        ? root._panelOpener
        : document.activeElement;
    dialog.querySelector('h2').textContent = heading;
    dialog.querySelector('.cr-panel-content').innerHTML = content;
    panelError('');
    patchMutationRecovery();
    if (!dialog.open) dialog.showModal();
    requestAnimationFrame(() => dialog.querySelector('[autofocus]')?.focus());
  }
  function projectOptions(selected = []) {
    const ids = new Set(selected.map((p) => (typeof p === 'string' ? p : p.project_id)));
    return S.projects.length
      ? S.projects
          .map(
            (project) =>
              `<label class="cr-choice"><input type="checkbox" name="project" value="${E(project.id)}" ${ids.has(project.id) ? 'checked' : ''}><span class="cr-choice-icon" aria-hidden="true">${icon('folder')}</span><span><strong>${E(project.alias || project.name || project.id)}</strong><small>${project.mode === 'write' ? '可修改' : '只读'}${project.allow_tasks ? ' · 可执行' : ''}</small></span></label>`,
          )
          .join('')
      : '<p class="cr-muted">暂无可关联项目，房间仍然可以创建和聊天。</p>';
  }
  function createPanel() {
    openPanel(
      'create',
      '新建房间',
      `<form data-cr-form="create">${field('房间名称', '<input name="title" required maxlength="120" placeholder="例如：产品工作室" autofocus>')}<fieldset><legend>项目 <span>可稍后添加</span></legend>${projectOptions(requestedProject ? [requestedProject] : [])}</fieldset><button class="cr-button cr-primary" type="submit">创建房间</button></form>`,
    );
  }
  function projectsPanel() {
    if (!state.room) return;
    openPanel(
      'projects',
      '房间项目',
      `<form data-cr-form="projects"><p class="cr-muted">关联项目后，可在成员里分配参与范围。</p><fieldset><legend class="sr-only">关联项目</legend>${projectOptions(state.room.projects || [])}</fieldset><div class="cr-impact" data-cr-project-impact hidden></div><button class="cr-button cr-primary" type="submit">保存项目</button></form>`,
      { version: state.room.version },
    );
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
                  `<article class="cr-member"><span class="cr-member-avatar" aria-hidden="true">${E((member.name || member.label || 'D').slice(0, 1))}</span><div><h3>${E(member.name || member.label)}${state.room.lead_member_id === memberId(member) ? '<span class="cr-tag">主责</span>' : ''}</h3><p>${E(title(member))}</p><small>${E(memberShort(member))} · ${E(memberOrigin(member))}</small></div>${btn('member-detail', '管理', `data-id="${E(memberId(member))}" aria-label="管理 ${E(member.name || member.label)} ${E(memberShort(member))}"`)}</article>`,
              )
              .join('')
          : '<p class="cr-muted">连接一个 dot，让工作持续推进。</p>'
      }</div>${btn('add-member', '添加 dot', '', 'cr-primary cr-full')}`,
    );
  }
  function scopeOptions(existing) {
    return (
      (state.room?.projects || [])
        .filter((p) => p.state !== 'removed')
        .map((project) => {
          const candidate = S.projects.find((p) => p.id === project.project_id);
          const capabilities = project.capabilities?.length
            ? project.capabilities
            : [
                'read',
                ...(candidate?.mode === 'write' ? ['write'] : []),
                ...(candidate?.allow_tasks ? ['execute'] : []),
              ];
          const prior = existing?.find(
            (item) =>
              item.project_id === project.project_id &&
              (item.environment_id || 'production') === (project.environment_id || 'production'),
          );
          const selected = existing ? prior?.capabilities || [] : ['read'];
          const targets = prior?.execution_targets?.length
            ? prior.execution_targets
            : ['project_agent'];
          const lifetime = !prior
            ? '新项目：直到我撤销'
            : prior.lifetime?.mode === 'until_revoked'
              ? '原有效期：直到我撤销'
              : prior.lifetime?.mode === 'timed' && prior.lifetime.expires_at
                ? '原有效期：' +
                  new Date(prior.lifetime.expires_at * 1000).toLocaleString('zh-CN') +
                  ' 截止'
                : '原有效期：需要核对';
          return `<fieldset class="cr-scope" data-project-id="${E(project.project_id)}" data-environment-id="${E(project.environment_id || 'production')}" data-execution-targets="${E(JSON.stringify(targets))}"><legend>${E(project.project_label || candidate?.alias || project.project_id)}${project.environment_id && project.environment_id !== 'production' ? ' · ' + E(project.environment_id) : ''}</legend><div>${['read', 'write', 'execute'].map((cap) => `<label><input type="checkbox" name="capability" value="${cap}" ${selected.includes(cap) ? 'checked' : ''} ${capabilities.includes(cap) ? '' : 'disabled'}>${{ read: '读取', write: '修改', execute: '执行' }[cap]}</label>`).join('')}</div><small class="cr-muted">执行位置：${E(targets.map((target) => (target === 'project_agent' ? '项目 Agent' : target)).join('、'))}</small>${existing ? `<small class="cr-muted" data-cr-scope-lifetime>${E(lifetime)}</small>` : ''}</fieldset>`;
        })
        .join('') || '<div class="cr-info">当前没有项目。dot 接入后可在房间聊天。</div>'
    );
  }
  function addMemberPanel() {
    openPanel(
      'add-member',
      '添加 dot',
      `<form data-cr-form="member"><fieldset class="cr-connection-mode"><legend class="sr-only">选择连接方式</legend><label><input type="radio" name="connection_mode" value="new" checked><span>新 dot</span></label><label><input type="radio" name="connection_mode" value="existing"><span>已有 dot</span></label></fieldset><section data-cr-connections hidden>${field('已验证的连接', '<select name="reuse_member_id" disabled><option value="">选择已验证的 dot</option></select>')}<p class="cr-muted" data-cr-connection-status></p>${btn('more-connections', '读取已有连接', 'data-cr-more-connections', 'cr-text-button')}<p class="cr-info">此房间会获得独立的成员身份与下方授权。创建后，需在所选的原 dot 中确认并验证连接。</p></section>${field('成员名称', '<input name="name" required maxlength="80" placeholder="给这个 dot 起个名字" autofocus>')}<section class="cr-scopes"><h3>参与范围</h3>${scopeOptions()}</section>${field('有效期', '<select name="lifetime"><option value="until_revoked">直到我撤销</option><option value="timed">7 天</option></select>')}<p class="cr-muted">创建后，把加入说明发给目标 dot；连接进度会自动更新。</p><button class="cr-button cr-primary" type="submit">添加并连接</button></form>`,
      {
        version: state.room.version,
        connections: [],
        connectionsCursor: '',
        connectionsLoaded: false,
      },
    );
  }
  async function loadConnections(more = false) {
    if (panel?.name !== 'add-member' || panel.connectionsLoading) return;
    const current = panel;
    current.connectionsLoading = true;
    const status = root.querySelector('[data-cr-connection-status]');
    status.textContent = '正在读取你已验证的 dot…';
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
      status.textContent = current.connections.length
        ? '这里只列出当前账号已完成往返验证的原连接。'
        : current.connectionsCursor
          ? '当前页没有可复用的连接，可以继续查看。'
          : '还没有其他房间的已验证连接。先添加一个新 dot 并完成连接。';
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
  function memberScopePanel(member) {
    openPanel(
      'member-scope',
      '参与范围 · ' + (member.name || member.label),
      `<form data-cr-form="member-scope"><section class="cr-scopes">${scopeOptions(member.project_scopes || [])}</section>${field('有效期调整', '<select name="lifetime"><option value="preserve">保持原有效期（新项目直到我撤销）</option><option value="until_revoked">所有所选项目重新设为：直到我撤销</option><option value="timed">所有所选项目重新设为：从现在起 7 天</option></select>')}<p class="cr-muted">默认保留各项目原期限。保存只调整此成员的房间范围；已移出项目的历史仍按当前权限查看。</p><button class="cr-button cr-primary" type="submit">保存参与范围</button></form>`,
      { member, version: state.room.version },
    );
  }
  function formScopes(form) {
    return [...form.querySelectorAll('[data-project-id]')]
      .map((group) => {
        const capabilities = [...group.querySelectorAll('input:checked:not(:disabled)')].map(
          (input) => input.value,
        );
        return {
          project_id: group.dataset.projectId,
          environment_id: group.dataset.environmentId || 'production',
          capabilities,
          execution_targets: capabilities.includes('execute')
            ? JSON.parse(group.dataset.executionTargets)
            : [],
        };
      })
      .filter((item) => item.capabilities.length);
  }
  function selectedProjects(ids) {
    return ids.flatMap((project) => {
      const existing = (state.room?.projects || []).filter((item) => item.project_id === project);
      return existing.length
        ? existing.map((item) => ({ project, environment_id: item.environment_id }))
        : [{ project, environment_id: 'production' }];
    });
  }
  async function memberPanel(id) {
    const member = members().find((m) => memberId(m) === id);
    if (!member) return;
    openPanel(
      'member',
      (member.name || member.label) + ' · ' + memberShort(member),
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
    const markup = `<div class="cr-flow-state" data-stage="${E(stage)}"><span class="cr-flow-symbol ${ready ? 'is-ready' : ''}" aria-hidden="true">${ready ? '✓' : icon('network')}</span><h3>${E(flow.display?.title || title(flow))}</h3>${flow.display?.reason ? `<p>${E(flow.display.reason)}</p>` : ''}</div>${flow.pending_binding ? '<p class="cr-info">新连接验证通过后才会替换当前连接。原成员和工作保持原来的身份。</p>' : member?.connection_source || member?.connection_source_member_id ? '<p class="cr-info">请在已选择的原 dot 中完成本房间的独立接入与验证。此房间的参与范围单独生效。</p>' : ''}${panel.joinInstructions ? `<label class="cr-field"><span>加入说明</span><textarea class="cr-instructions" readonly rows="7" aria-label="dot 加入说明"></textarea></label>${btn('copy-instructions', '复制加入说明', '', 'cr-primary cr-full')}` : ''}${primary && !['copy_instructions', 'copy_join_instructions', 'send_message', 'start_work', 'none'].includes(primary) ? btn('flow-action', actionLabel(primary), `data-flow-action="${E(primary)}"`, 'cr-primary cr-full') : ''}${member ? `<section class="cr-member-controls">${btn('member-scope', '参与范围')}${member.legacy ? btn('flow-action', '迁移此连接', 'data-flow-action="migrate"') : member.state !== 'removed' ? btn('rebind-member', flow.pending_binding ? '查看更换说明' : '更换连接') : ''}${state.room?.lead_member_id !== memberId(member) && member.state !== 'removed' ? btn('lead', '设为主责', `data-id="${E(memberId(member))}"`) : ''}${member.state !== 'removed' && member.state !== 'paused' ? btn('flow-action', '暂停', 'data-flow-action="pause"') : ''}${member.state !== 'removed' ? btn('remove-member', '移除成员', '', 'cr-danger') : ''}</section>` : ''}<details class="cr-diagnostics"><summary>连接详情</summary><dl>${Object.entries(
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
  async function mutation(method, url, body, current = panel) {
    const unresolved = [...state.mutations.values()].find(
      (item) => item.method === method && item.url === url,
    );
    if (unresolved) {
      await recoverAction(unresolved.id);
      return null;
    }
    const epoch = state.epoch,
      owner = state.owner;
    const requestBody = JSON.stringify({
      ...body,
      idempotency_key: body.idempotency_key || current?.key || store.key(),
    });
    const kind =
      url === base + '/rooms'
        ? 'create'
        : url.endsWith('/members')
          ? 'member'
          : url.includes('/connect-flows/')
            ? 'flow'
            : url.includes('/channel-connect-flows/') || url.endsWith('/channels/weixin/connect')
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
      }[kind],
      state: 'unknown',
    };
    if (current) {
      current.busy = true;
      current.writeEpoch = (current.writeEpoch || 0) + 1;
      panelError('');
    }
    const forms = [...(root?.querySelectorAll('dialog form') || [])];
    for (const form of forms) {
      form.setAttribute('aria-busy', 'true');
      for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = true;
    }
    const actionButtons = [
      ...(root?.querySelectorAll('.cr-panel-content button[data-cr-action]') || []),
    ];
    for (const button of actionButtons) button.disabled = true;
    try {
      const response = await api(url, {
        method,
        body: requestBody,
      });
      if (owner !== state.owner) return null;
      if (epoch === state.epoch && response.room) {
        store.invalidateReads();
        store.applyRoom(response);
      }
      if (current && current !== panel) return null;
      if (current) current.key = store.key();
      return response;
    } catch (error) {
      if (owner !== state.owner) throw error;
      if (error.code === 'NETWORK_UNCERTAIN' || error.status >= 500) {
        state.mutations.set(receipt.id, receipt);
        patchPending();
      }
      if (current === panel) {
        panelError(error.message);
        if (error.status === 409) {
          await store.refresh().catch(() => {});
          panelError('房间刚有更新。你的选择已保留，请核对后再次保存。');
          if (panel && state.room) {
            panel.version = state.room.version;
            panel.key = store.key();
            if (panel.name === 'work') {
              const latest = state.works.find((work) => work.id === panel.work.id);
              if (latest) renderWork(latest);
              panelError('原工作刚有更新，请核对当前连接和原操作后再继续。');
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
    }
  }
  async function recoverAction(id) {
    const receipt = state.mutations.get(id);
    if (!receipt || receipt.state === 'recovering' || receipt.owner !== state.owner) return;
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
        if (!saved) receipt.state = 'unknown';
        const message =
          (saved ? '原操作已保存，视图需要重新读取：' : '原操作尚未确认：') + error.message;
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
    } else if (type === 'projects') {
      const result = await mutation('PUT', store.path() + '/projects', {
        expected_version: current.version,
        projects: selectedProjects(data.getAll('project')),
      });
      if (!result) return;
      closePanel();
      await store.refresh();
      announce('房间项目已更新');
    } else if (type === 'member' || type === 'member-scope') {
      const project_scopes = formScopes(form);
      const lifetime =
        type === 'member-scope' && data.get('lifetime') === 'preserve'
          ? null
          : data.get('lifetime') === 'timed'
            ? { mode: 'timed', expires_at: Math.floor(Date.now() / 1000) + 7 * 86400 }
            : { mode: 'until_revoked' };
      if (type === 'member-scope') {
        const result = await mutation('PUT', store.path() + '/projects', {
          expected_version: current.version,
          projects: (state.room.projects || []).map((project) => ({
            project: project.project_id,
            environment_id: project.environment_id,
          })),
          member_approvals: [
            {
              member_id: memberId(current.member),
              project_scopes,
              ...(lifetime ? { lifetime } : {}),
            },
          ],
        });
        if (result) {
          await store.refresh();
          await memberPanel(memberId(current.member));
        }
        return;
      }
      const result = await mutation('POST', store.path() + '/members', {
        name: data.get('name').trim(),
        expected_version: current.version,
        project_scopes,
        lifetime,
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
  function settingsPanel() {
    const room = state.room;
    openPanel(
      'settings',
      '房间设置',
      `<form data-cr-form="settings">${field('房间名称', `<input name="title" value="${E(room.title)}" required maxlength="120" autofocus>`)}<button type="submit" class="cr-button cr-primary">保存名称</button></form><section class="cr-setting-actions">${btn('room-state', room.state === 'paused' ? '继续房间' : '暂停房间', `data-state="${room.state === 'paused' ? 'active' : 'paused'}"`)}${btn('archive-room', room.state === 'archived' ? '恢复房间' : '归档房间', '', 'cr-danger')}${btn('diagnostics', '运行诊断')}</section>`,
      { version: room.version },
    );
  }
  function workPanel(id) {
    const work = state.works.find((item) => item.id === id);
    if (!work) return;
    openPanel('work', work.title || '当前工作', '<div data-cr-work-view></div>', { work });
    renderWork(work);
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
    const actions = work.allowed_actions || work.display?.allowed_actions || [];
    const recovery = work.recovery || {};
    const coordinatorId = work.coordinator_member_id || recovery.coordinator_member_id;
    const team = work.members || [];
    const coordinator = team.find((member) => (member.member_id || member.id) === coordinatorId);
    const operations = work.operations || [];
    const reviewRevision = JSON.stringify([
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
    const markup = `<div class="cr-flow-state"><h3>${E(work.display?.title || workLabels[work.state] || work.state)}</h3><p>${E(work.display?.reason || work.reason_code || '')}</p></div>${work.result_summary ? `<p>${E(work.result_summary)}</p>` : ''}
      <section class="cr-work-summary"><h3>验收条件</h3><p>${E(work.acceptance || '此工作未单列验收条件。')}</p>${coordinatorId ? `<p data-cr-work-coordinator><strong>当前主责</strong> ${E(coordinator?.name || recovery.coordinator_name || 'dot')} · #${E(coordinatorId.slice(0, 8))}</p>` : ''}${team.length ? `<h3>参与成员</h3><ul>${team.map((member) => `<li>${E(member.name || 'dot')} · #${E((member.member_id || member.id).slice(0, 8))}${(member.member_id || member.id) === coordinatorId ? ' · 主责' : ''}</li>`).join('')}</ul>` : ''}</section>
      <details class="cr-diagnostics cr-work-evidence" open><summary>步骤与原操作</summary>${work.items?.length ? `<ul>${work.items.map((item) => `<li><strong>${E(item.title || '工作步骤')}</strong> · ${E(workLabels[item.state] || item.state)}${item.member_id ? ' · #' + E(item.member_id.slice(0, 8)) : ''}${item.display?.reason || item.reason_code ? `<p>${E(item.display?.reason || item.reason_code)}</p>` : ''}<small>步骤 #${E(item.id.slice(0, 8))}</small></li>`).join('')}</ul>` : '<p class="cr-muted">当前没有可显示的执行步骤。</p>'}<div data-cr-operation-receipts>${operations.length ? `<ul>${operations.map((operation) => `<li data-operation-id="${E(operation.id || operation.operation_id)}"><strong>${E(operation.display?.title || workLabels[operation.state] || operation.state)}</strong>${operation.cancel_requested && !['succeeded', 'failed', 'cancelled'].includes(operation.state) ? '<p>已请求停止，等待原操作回执</p>' : ''}${operation.display?.reason ? `<p>${E(operation.display.reason)}</p>` : ''}<small>原操作 #${E(String(operation.id || operation.operation_id || '').slice(0, 12))}</small></li>`).join('')}</ul>` : '<p class="cr-muted">当前没有已记录的操作；不会把未开始显示为已完成。</p>'}</div></details>
      ${recovery.state === 'waiting_operations' ? '<p class="cr-info">先核对上面的原操作回执，确认结束后才能恢复或接替。</p>' : ''}
      ${actions.includes('restore_binding') && recovery.restore_candidates?.length ? workRecoveryForm('restore_binding', recovery.restore_candidates) : ''}
      ${actions.includes('takeover') && recovery.takeover_candidates?.length ? workRecoveryForm('takeover', recovery.takeover_candidates) : ''}
      <div class="cr-work-controls">${actions
        .filter((action) => ['pause', 'resume', 'cancel'].includes(action))
        .map((action) =>
          btn(
            'work-control',
            { pause: '暂停工作', resume: '继续原工作', cancel: '停止工作' }[action],
            `data-work-action="${action}"`,
            action === 'cancel' ? 'cr-danger' : 'cr-primary',
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
      panelError('工作或连接范围已变化，请核对原操作后重新选择成员。');
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
    const result = await mutation('POST', store.path() + '/channels/weixin/connect', {
      ...(state.room.lead_member_id ? { member_id: state.room.lead_member_id } : {}),
    });
    if (result && panel === current) renderChannel(result);
  }
  function renderChannel(result) {
    if (!panel || !root) return;
    const flow = result.flow || result;
    panel.channel = flow;
    panel.flowId = flow.id;
    const box = root.querySelector('[data-cr-channel-view]');
    if (!box) return;
    const next = flow.next_request || {},
      fields = next.form_fields || [];
    const action = next.body?.action || flow.primary_action;
    const accounts = flow.stage === 'choose_account' ? flow.accounts || [] : [];
    const qr = flow.qr?.qrcode_image;
    const safeQr =
      typeof qr === 'string' &&
      qr.length <= 1500000 &&
      /^data:image\/svg\+xml;base64,[A-Za-z0-9+/]+={0,2}$/.test(qr);
    const label =
      flow.actions?.find((item) => item.action === action)?.label || actionLabel(action);
    const parts = [
      `<div class="cr-flow-state"><span class="cr-flow-symbol ${flow.stage === 'ready' ? 'is-ready' : ''}" aria-hidden="true">${flow.stage === 'ready' ? '✓' : icon('message')}</span><h3>${E(flow.display?.title || title(flow))}</h3><p>${E(flow.display?.reason || flow.message || '')}</p></div>`,
      `<ol class="cr-channel-steps" aria-label="连接微信进度"><li class="${['choose_account', 'scan', 'confirm_account'].includes(flow.stage) ? 'is-current' : ''}">扫码</li><li class="${['confirm_binding', 'pairing', 'pairing_expired'].includes(flow.stage) ? 'is-current' : ''}">确认本人</li><li class="${['confirm_owner', 'ready'].includes(flow.stage) ? 'is-current' : ''}">完成连接</li></ol>`,
    ];
    if (accounts.length)
      parts.push(
        `<section class="cr-channel-existing">${field('已连接的微信', `<select name="existing_account_id"><option value="">选择账号</option>${accounts.map((account) => `<option value="${E(account.id)}">${E(account.label || account.bot_id)}</option>`).join('')}</select>`)}${btn('channel-existing', '使用此账号', '', 'cr-full')}</section>`,
      );
    if (safeQr)
      parts.push(
        `<img class="cr-qr" src="${E(qr)}" alt="用微信扫描二维码登录" referrerpolicy="no-referrer">`,
      );
    if (flow.qr?.scanner_id)
      parts.push(`<div class="cr-info">扫码身份：${E(flow.qr.scanner_id)}</div>`);
    if (flow.binding?.pairing_challenge)
      parts.push(
        `<div class="cr-info"><strong>在这个微信私聊发送</strong><p class="cr-pairing-code">${E(flow.binding.pairing_challenge)}</p><p>收到后会自动进入本人确认。</p></div>`,
      );
    if (flow.existing_binding)
      parts.push(
        `<div class="cr-info">此微信已有房间绑定。确认迁移后，新消息进入当前房间；原工作与回复保留在原处。</div>`,
      );
    if (flow.task_scope) {
      const preview = flow.task_scope;
      const projects =
        preview.projects || preview.project_scopes || (preview.project_id ? [preview] : []);
      parts.push(
        `<details class="cr-diagnostics" open><summary>允许工作的范围</summary><p>${E(preview.summary || preview.intent || '按以下已批准项目参与协作')}</p><ul>${projects.map((project) => `<li>${E(project.project_label || project.alias || S.projects.find((item) => item.id === project.project_id)?.alias || project.project_id || project)}${project.capabilities ? ' · ' + E(project.capabilities.map((cap) => ({ read: '读取', write: '修改', execute: '执行' })[cap] || cap).join('、')) : ''}${project.execution_targets?.length ? '<br>执行位置：' + E(project.execution_targets.map((target) => (target === 'project_agent' ? '项目 Agent' : target)).join('、')) : ''}</li>`).join('')}</ul>${preview.max_delegations !== undefined ? `<p>累计工作额度：${E(preview.max_delegations)}</p>` : ''}<p>${E(preview.result_destination || '结果回复到原私聊及当前房间')}</p></details>`,
      );
    }
    if (flow.stage === 'ready')
      parts.push(
        `<p class="cr-info">${E(flow.account?.label || '微信')} · ${E(state.room?.title || '')}<br>${flow.work_authorized ? '已允许本人交办工作' : '已连接房间聊天'}</p>`,
      );
    if ((flow.actions || []).some((item) => item.action === 'revoke_tasks'))
      parts.push(
        '<p class="cr-info">可以仅撤销这个微信的工作许可，保留聊天连接。新的微信任务与后续步骤会停止准入；已启动操作继续核对原回执。</p>',
      );
    if (action && (flow.stage !== 'ready' || !['refresh', ''].includes(action)))
      parts.push(
        `<form data-cr-form="weixin" data-channel-action="${E(action)}">${fields.map(channelField).join('')}${flow.qr?.re_pair_required && action === 'accept_account' ? channelField({ name: 'confirm_replace_existing', type: 'checkbox', required: true, label: '确认替换此账号的原连接' }) : ''}${flow.stage === 'confirm_owner' ? `<div class="cr-info"><strong>${E(state.room?.title)}</strong><p>微信联系人：${E(flow.binding?.pending_peer_id || '')}</p></div>` : ''}<button type="submit" class="cr-button cr-primary cr-full">${E(label)}</button></form>`,
      );
    const secondary = (flow.actions || []).filter((item) => item.action !== action);
    if (secondary.length)
      parts.push(
        `<div class="cr-setting-actions">${secondary.map((item) => btn('channel-action', E(item.action === 'revoke_tasks' ? '仅撤销工作许可' : item.label), `data-channel-action="${E(item.action)}"`, ['disconnect', 'revoke_tasks'].includes(item.action) ? 'cr-danger' : '')).join('')}</div>`,
      );
    const markup = parts.join('');
    const consentRevision = JSON.stringify([
      flow.id,
      flow.version,
      flow.account?.id,
      flow.account?.credential_version,
      flow.binding?.id,
      flow.binding?.version,
      flow.binding?.generation,
      flow.binding?.pending_peer_id,
      flow.task_scope?.scope_digest,
      flow.qr?.scanner_id,
      flow.qr?.re_pair_required,
      flow.existing_binding?.id,
      flow.existing_binding?.version,
    ]);
    const changedConsent = box._consentRevision && box._consentRevision !== consentRevision;
    const checkedBeforeChange =
      changedConsent && !!box.querySelector('input[type="checkbox"]:checked');
    if (box._markup !== markup || changedConsent) {
      box.innerHTML = markup;
      box._markup = markup;
    }
    box._consentRevision = consentRevision;
    if (checkedBeforeChange) panelError('连接身份或参与范围已变化，请核对后重新确认。');
    updateChannelDependencies();
  }
  function updateChannelDependencies() {
    const form = root?.querySelector('[data-cr-form="weixin"]');
    if (!form) return;
    for (const control of form.querySelectorAll('[data-depends-on]')) {
      const enabled = form.elements.namedItem(control.dataset.dependsOn)?.checked === true;
      control.disabled = !enabled;
      control.required = enabled;
      control.closest('label').hidden = !enabled;
      if (!enabled) control.checked = false;
    }
  }
  function channelField(item) {
    const name = item.name || item.key,
      label = item.label || name;
    if (!name) return '';
    if (item.options)
      return field(
        E(label),
        `<select name="${E(name)}" ${item.required ? 'required' : ''}>${item.options.map((option) => `<option value="${E(option.value ?? option.id)}">${E(option.label || option.name)}</option>`).join('')}</select>`,
      );
    if (item.type === 'checkbox' || item.type === 'boolean')
      return `<label class="cr-choice"><input type="checkbox" name="${E(name)}" ${item.required ? 'required' : ''} ${item.depends_on ? `data-depends-on="${E(item.depends_on)}"` : ''}><span>${E(label)}</span></label>`;
    return field(
      E(label),
      `<input name="${E(name)}" value="${E(item.value || '')}" ${item.required ? 'required' : ''} autocomplete="off">`,
    );
  }
  async function submitChannel(form) {
    const flow = panel.channel,
      next = flow.next_request || {},
      values = Object.fromEntries(new FormData(form));
    for (const input of form.querySelectorAll('input[type="checkbox"]'))
      values[input.name] = input.checked;
    const body = {
      ...(next.body || {}),
      ...values,
      action: form.dataset.channelAction,
      expected_version: flow.version,
      ...(flow.task_scope?.scope_digest
        ? { expected_scope_digest: flow.task_scope.scope_digest }
        : {}),
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
      if (flow.primary_action === 'poll_qr' && !(flow.next_request?.form_fields || []).length) {
        const response = await mutation(
          'POST',
          base + '/channel-connect-flows/' + encodeURIComponent(flow.id) + '/actions',
          {
            ...flow.next_request.body,
            idempotency_key: flow.id + ':poll:' + flow.version,
          },
          current,
        );
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
    } else if (result.not_saved === true) await store.send(receipt);
    else announce('原消息仍在核对，恢复连接后继续查看。');
  }
  async function click(button) {
    const action = button.dataset.crAction,
      id = button.dataset.id;
    if (action === 'create') createPanel();
    else if (action === 'more-rooms') await store.list({ more: true });
    else if (action === 'archives') await store.list({ includeArchived: !state.includeArchived });
    else if (action === 'select') await selectRoom(id);
    else if (action === 'rooms') {
      const visible = root.classList.toggle('cr-show-rooms');
      button.setAttribute('aria-expanded', String(visible));
    } else if (action === 'projects') projectsPanel();
    else if (action === 'members') membersPanel();
    else if (action === 'add-member') addMemberPanel();
    else if (action === 'more-connections') await loadConnections(!!panel.connectionsCursor);
    else if (action === 'member-detail') await memberPanel(id);
    else if (action === 'member-scope') memberScopePanel(panel.member);
    else if (action === 'close') closePanel();
    else if (action === 'settings') settingsPanel();
    else if (action === 'weixin') await weixinPanel();
    else if (action === 'channel-existing') {
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
      const feed = root.querySelector('.cr-feed'),
        height = feed.scrollHeight,
        top = feed.scrollTop;
      root._loadingOlder = true;
      try {
        await store.messages(true);
        feed.scrollTop = top + feed.scrollHeight - height;
      } finally {
        if (root) root._loadingOlder = false;
      }
    } else if (action === 'latest') {
      const feed = root.querySelector('.cr-feed');
      feed.scrollTop = feed.scrollHeight;
      button.hidden = true;
    } else if (action === 'recover-send') await recoverSend(id);
    else if (action === 'recover-action') await recoverAction(id);
    else if (action === 'flow-action') {
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
          refresh_invitation: 'refresh_invite',
        }[requestAction] || requestAction;
      const result = await mutation(
        'POST',
        base + '/connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: mapped, expected_version: flow.version },
      );
      if (result) {
        renderFlow(result);
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
      const result = await mutation(
        'POST',
        base + '/connect-flows/' + encodeURIComponent(panel.flowId) + '/actions',
        { action: 'remove', expected_version: panel.flow.version },
      );
      if (result) {
        await store.refresh();
        membersPanel();
      }
    } else if (action === 'lead') {
      const result = await mutation('PATCH', store.path(), {
        lead_member_id: id,
        expected_version: state.room.version,
      });
      if (result) {
        await store.refresh();
        membersPanel();
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
          '<p>停止新工作并保留历史。归档后可以恢复这个房间。</p>' +
            btn('room-state', '归档房间', 'data-state="archived"', 'cr-danger cr-full'),
        );
    } else if (action === 'work-detail') workPanel(id);
    else if (action === 'work-control') await controlWork(button.dataset.workAction);
    else if (action === 'supplement') {
      store.draft().work = panel.work.id;
      store.draft().workVersion = panel.work.version;
      closePanel();
      patchComposer();
      root.querySelector('#cr-message').focus();
      announce('下一条消息将补充到这项工作');
    } else if (action === 'diagnostics') {
      openPanel('diagnostics', '运行诊断', '<p class="cr-muted">正在读取…</p>');
      const current = panel;
      const data = await api(store.path() + '/diagnostics');
      if (current === panel)
        root.querySelector('.cr-panel-content').innerHTML =
          `<pre class="cr-diagnostic-output">${E(JSON.stringify(data, null, 2))}</pre>`;
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
      closeHints();
      store.abort();
      root._restoringReading = false;
      closePanel();
      root.querySelector('.cr-panel-content').replaceChildren();
      const pending = root.querySelector('[data-cr-pending]');
      pending.replaceChildren();
      pending._markup = null;
      state.room = null;
      state.messages = [];
      state.works = [];
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
    const epoch = state.epoch;
    pollPromise = (async () => {
      try {
        await store.refresh();
        if (epoch !== state.epoch) return;
        if (panel?.name === 'member') await refreshFlow();
        if (panel?.name === 'weixin') await refreshChannel();
      } catch (error) {
        if (error.name !== 'AbortError' && root)
          announce('刷新暂时失败，草稿与原工作已保留。' + error.message, true);
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
            draft.files.some((entry) => entry.state !== 'ready')
          )
            return;
          if (
            [...state.pending.values()].some(
              (item) => item.roomId === state.roomId && store.sameDraft(item),
            )
          )
            return;
          store.send().catch((error) => {
            if (state.roomId === roomId && state.owner === owner) announce(error.message, true);
          });
        } else if (event.target.matches('[data-cr-form]')) submitForm(event.target).catch(() => {});
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
        if (event.target.name === 'member_id') store.draft().member = event.target.value;
        if (event.target.name === 'allow_owner_tasks') updateChannelDependencies();
        if (event.target.name === 'capability') {
          const group = event.target.closest('[data-project-id]');
          if (event.target.value === 'read' && !event.target.checked) {
            for (const input of group.querySelectorAll('input')) input.checked = false;
          } else if (event.target.checked) group.querySelector('[value="read"]').checked = true;
        }
        if (event.target.name === 'project' && panel?.name === 'projects') {
          const chosen = new Set(
            [...event.target.form.querySelectorAll('[name="project"]:checked')].map(
              (item) => item.value,
            ),
          );
          const removed = (state.room.projects || []).filter(
            (item) => !chosen.has(item.project_id),
          );
          const affected = state.works.filter(
            (work) =>
              !['completed', 'succeeded', 'cancelled', 'failed'].includes(work.state) &&
              work.project_ids?.some((id) => removed.some((project) => project.project_id === id)),
          );
          const node = root.querySelector('[data-cr-project-impact]');
          node.hidden = !removed.length;
          node.textContent = removed.length
            ? `将移出 ${removed.map((item) => item.project_label || S.projects.find((p) => p.id === item.project_id)?.alias || item.project_id).join('、')}。该范围的新步骤会停止准入，项目文件保留。${affected.length ? '受影响的工作：' + affected.map((work) => work.title).join('、') + '。' : ''}`
            : '';
        }
      },
      { signal },
    );
    root.querySelector('dialog').addEventListener(
      'close',
      () => {
        panelEpoch++;
        panel = null;
        const dialog = root?.querySelector('dialog');
        const opener = dialog?._crOpener;
        if (opener?.isConnected && !opener.disabled) opener.focus({ preventScroll: true });
      },
      { signal },
    );
    root.querySelector('.cr-feed').addEventListener(
      'scroll',
      () => {
        const feed = root.querySelector('.cr-feed');
        if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90)
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
    if (S.page === 'collaboration') {
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
  return { html, bind, detach, clear, open };
})();
