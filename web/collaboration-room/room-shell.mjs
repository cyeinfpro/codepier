// Room shell: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  btn,
  contextCorrectionStale,
  currentOwner,
  detach,
  hue,
  icon,
  identifier,
  memberBadges,
  memberId,
  memberShort,
  members,
  migrationBlockerMarkup,
  panel,
  patchFiles,
  patchRoomPermissions,
  readOnlyReason,
  renderMigration,
  requestedProject,
  roomChip,
  roomFilter,
  roomName,
  roomProjects,
  roomSubtitle,
  root,
  selection,
  state,
  store,
  supplied,
} from './index.mjs';

export function clear() {
  detach();
  store.reset();
}

export async function html() {
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
      <form class="cr-composer" id="cr-composer"><div class="cr-reply cr-context-correction" data-cr-correction hidden></div><div class="cr-reply" data-cr-reply hidden></div><div class="cr-draft-files" data-cr-files aria-label="待发送的附件" aria-live="polite" hidden></div><div class="cr-composer-line"><textarea id="cr-message" name="body_text" aria-label="消息" placeholder="继续对话，或交办工作…" rows="1" maxlength="8000"></textarea><button type="submit" class="cr-send" aria-label="发送消息">${icon('arrow')}</button></div><footer><div class="cr-composer-tools">${btn('attach', icon('plus'), 'aria-label="添加附件" title="每个附件最多 8 MiB"', 'cr-icon cr-attach')}<input type="file" id="cr-file" multiple hidden aria-label="选择附件"><label class="cr-recipient"><span>发送给</span><select name="member_id" aria-label="选择处理成员"><option value="">主责 dot</option></select></label></div><span data-cr-compose-status>Enter 发送 · Shift + Enter 换行</span></footer></form>
    </main><dialog class="cr-panel" aria-labelledby="cr-panel-title"><header><h2 id="cr-panel-title"></h2>${btn('close', icon('close'), 'aria-label="关闭面板"', 'cr-icon')}</header><div class="cr-panel-content"></div><p role="alert" class="cr-error" data-cr-panel-error hidden></p><div class="cr-mutation-recovery" data-cr-panel-recovery hidden></div></dialog>
  </section>`;
}

export function patchRooms() {
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

export function patchHeader() {
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

export function patchEmpty() {
  const node = root?.querySelector('[data-cr-empty]');
  if (!node) return;
  const show = !state.messages.length && (!!state.room?.legacy || !state.works.length);
  root.querySelector('[data-cr-legacy]').hidden = !state.room?.legacy || !state.messages.length;
  node.hidden = !show;
  const activeProjectCount = (state.room?.projects || []).filter(
    (p) => p.state !== 'removed',
  ).length;
  const markup = !state.room
    ? `<div class="cr-empty"><div class="cr-orbit" aria-hidden="true"><span></span><i>${icon('message')}</i><span></span></div><h3>还没有房间</h3><p>新建房间后就可以添加项目和 dot。</p><div class="cr-empty-actions">${btn('create', '新建房间', '', 'cr-primary')}${btn('host-list', '管理 dot 连接', '', 'cr-text-button')}</div></div>`
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

export function migrationChecklist() {
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

export function patchComposer() {
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
            ? '发送后更新原交办'
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
    const markup = `<span>${draft.work ? '修改交办 · ' + E(work?.title || '当前工作') : '回复：' + E((original?.body_text || '原消息').slice(0, 100))}</span>${btn(draft.work ? 'cancel-supplement' : 'cancel-reply', '×', `aria-label="${draft.work ? '取消修改交办' : '取消引用'}"`, 'cr-icon')}`;
    if (reply._markup !== markup) {
      reply.innerHTML = markup;
      reply._markup = markup;
    }
  }
}
