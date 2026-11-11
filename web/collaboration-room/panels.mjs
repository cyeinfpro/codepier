// Panels: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  btn,
  field,
  memberId,
  members,
  openPanel,
  panel,
  panelError,
  readOnlyReason,
  root,
  safeChannelActions,
  safeWorkActions,
  state,
  store,
  supplied,
} from './index.mjs';

export function sizeEditor(editor) {
  editor.style.height = 'auto';
  editor.style.height = Math.min(editor.scrollHeight, 180) + 'px';
}

export function patchRoomPermissions() {
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

export function migrationRevision(room = state.room) {
  return JSON.stringify([room?.version, room?.legacy, room?.migration]);
}

export function migrationPanel() {
  if (!state.room) return;
  openPanel('migration', '启用新房间界面', '<div data-cr-migration-view></div>');
  renderMigration();
}

export function legacyChannelRemediation(blocker) {
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

export function migrationBlockerMarkup(blocker, inline = false) {
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

export function renderMigration() {
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

export function createPanel() {
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
