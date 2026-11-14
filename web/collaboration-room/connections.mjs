// Connections: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  actionLabel,
  activeRoom,
  announce,
  base,
  batchPath,
  btn,
  connectionChoice,
  connectionChoiceLabel,
  connectionListPath,
  connectionName,
  connectionRoomSummary,
  connectionState,
  dateTime,
  facts,
  field,
  hostBatchErrors,
  hostBatchLabels,
  hostImpactBinding,
  hostImpactLabel,
  hostPath,
  hostStates,
  icon,
  memberBadges,
  memberId,
  memberOrigin,
  memberShort,
  members,
  mutation,
  openPanel,
  panel,
  panelEpoch,
  panelError,
  patchRoomPermissions,
  readOnlyReason,
  refreshChannel,
  renderChannel,
  reuseAnchor,
  roomName,
  root,
  state,
  store,
  supplied,
  supportsHostReuse,
  title,
} from './index.mjs';

export function membersPanel() {
  if (!state.room) return;
  openPanel(
    'members',
    '房间成员',
    `<div class="cr-member-list">${
      members().length
        ? members()
            .map(
              (member) =>
                `<article class="cr-member"><span class="cr-member-avatar" aria-hidden="true">${E((member.name || member.label || 'D').slice(0, 1))}</span><div><h3>${E(member.name || member.label)}${state.room.lead_member_id === memberId(member) ? '<span class="cr-tag">主责</span>' : ''}</h3><p>${E(hostStates[member.host_connection_state] || memberBadges[member.stage || member.state] || '连接中')}</p><small>${E(memberOrigin(member))}</small></div>${btn('member-detail', '管理', `data-id="${E(memberId(member))}" aria-label="管理 ${E(member.name || member.label)} ${E(memberShort(member))}"`)}</article>`,
            )
            .join('')
        : '<p class="cr-muted">连接一个 dot，让工作持续推进。</p>'
    }</div>${btn('add-member', '添加 dot', '', 'cr-primary cr-full')}${btn('host-list', '管理 dot 连接', '', 'cr-text-button cr-full')}`,
  );
}

export async function addMemberPanel() {
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
    if (!initialized || roomId !== state.roomId || epoch !== state.epoch || opening !== panelEpoch)
      return;
    await store.refresh();
    if (roomId !== state.roomId || epoch !== state.epoch || opening !== panelEpoch) return;
  }
  openPanel(
    'add-member',
    '添加 dot',
    `<form data-cr-form="member"><fieldset class="cr-connection-mode"><legend class="sr-only">选择连接方式</legend><label><input type="radio" name="connection_mode" value="existing"><span>已有 dot</span></label><label><input type="radio" name="connection_mode" value="new" checked><span>新 dot</span></label></fieldset><section data-cr-connections hidden>${field('已验证的连接', '<select name="reuse_member_id" disabled><option value="">选择已验证的 dot</option></select>')}<p class="cr-muted" data-cr-connection-status></p>${btn('more-connections', '读取已有连接', 'data-cr-more-connections', 'cr-text-button')}<p class="cr-info">加入后使用房间的项目权限。</p></section>${field('成员名称', '<input name="name" required maxlength="80" placeholder="给这个 dot 起个名字" autofocus>')}<section class="cr-connect-summary" aria-label="接入范围"><p class="cr-muted">dot 加入「${E(roomName(state.room))}」并使用房间已批准的项目权限。读取和回复探测通过后就可在房间收发消息。</p><label class="cr-choice"><input type="checkbox" name="keep_connected"><span>在房间授权有效期间保持连接<small>自动处理可恢复的连接故障。订阅由 dot 宿主续订。</small></span></label></section><button class="cr-button cr-primary" type="submit">添加并连接</button></form>`,
    {
      version: state.room.version,
      connections: [],
      connectionsCursor: '',
      connectionsLoaded: false,
    },
  );
  void loadConnections();
}

export async function loadConnections(more = false) {
  if (panel?.name !== 'add-member' || panel.connectionsLoading) return;
  const current = panel;
  current.connectionsLoading = true;
  const status = root.querySelector('[data-cr-connection-status]');
  status.textContent = '正在加载已连接的 dot…';
  const moreButton = root.querySelector('[data-cr-more-connections]');
  moreButton.disabled = true;
  try {
    const cursor = more ? current.connectionsCursor : '';
    const useHost = current.connectionsUseHost !== false;
    let response = await api(connectionListPath(cursor, useHost));
    const capabilities = useHost ? response.connection_capabilities || {} : {};
    if (useHost && capabilities.host_reference === true && !response.next_cursor) {
      // Retain legacy anchors that have yet to be adopted. The two catalogs
      // use separate cursors.
      const legacy = await api(connectionListPath('', false));
      response = {
        items: [...(response.items || []), ...(legacy.items || [])],
        next_cursor: legacy.next_cursor,
      };
      current.connectionsUseHost = false;
    }
    const hostAnchors = new Set(
      [...current.connections, ...(response.items || [])]
        .filter((item) => item.host_connection_id)
        .map(reuseAnchor)
        .filter(Boolean),
    );
    const presentHosts = new Set(
      members()
        .map((member) => member.host_connection_id)
        .filter(Boolean),
    );
    const available = (response.items || []).filter((item) =>
      item.host_connection_id
        ? !presentHosts.has(item.host_connection_id) &&
          !item.room_ids?.includes(current.roomId) &&
          item.state !== 'revoked' &&
          !['authorization_expired', 'grant_expired'].includes(item.display_state) &&
          (supportsHostReuse(item, capabilities) || !!item.reuse_member_id)
        : item.conversation_id !== current.roomId &&
          !!reuseAnchor(item) &&
          !hostAnchors.has(reuseAnchor(item)),
    );
    if (current !== panel) return;
    current.connections = [
      ...new Map(
        [...(more ? current.connections : []), ...available].map((item) => [
          connectionChoice(item),
          item,
        ]),
      ).values(),
    ];
    if (useHost) current.connectionCapabilities = capabilities;
    current.connectionsCursor = response.next_cursor || '';
    current.connectionsLoaded = true;
    const select = root.querySelector('[name="reuse_member_id"]'),
      selected = select.value;
    select.innerHTML =
      '<option value="">选择已验证的 dot</option>' +
      current.connections
        .map(
          (item) =>
            `<option value="${E(connectionChoice(item))}">${E(connectionChoiceLabel(item))}</option>`,
        )
        .join('');
    if ([...select.options].some((option) => option.value === selected)) select.value = selected;
    const modes = [...root.querySelectorAll('[name="connection_mode"]')];
    if (!more && current.connections.length && !current.modeTouched) {
      const existing = modes.find((input) => input.value === 'existing');
      existing.checked = true;
      existing.dispatchEvent(new Event('change', { bubbles: true }));
      // Prefer an online verified connection, but never join without submit.
      const preferred = current.connections.find((item) =>
        ['connected', 'ready', 'ready_chat', 'ready_work'].includes(
          item.display_state || item.state,
        ),
      );
      if (!selected && preferred) {
        select.value = connectionChoice(preferred);
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
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

export function hostListPanel() {
  openPanel(
    'host-list',
    'dot 连接',
    '<p class="cr-info">旧房间可能各自登记了同名连接。每条连接独立管理，撤销前可核对受影响的房间。</p>' +
      '<div data-cr-host-list aria-live="polite"></div>' +
      btn('more-hosts', '更多连接', 'data-cr-more-hosts hidden', 'cr-text-button'),
    { hostCursor: '', hosts: [] },
  );
  return loadHostList();
}

export async function loadHostList() {
  const current = panel;
  if (current?.name !== 'host-list' || current.loading) return;
  current.loading = true;
  const box = root.querySelector('[data-cr-host-list]');
  const more = root.querySelector('[data-cr-more-hosts]');
  more.disabled = true;
  if (!current.hosts.length) box.textContent = '正在读取 dot 连接…';
  try {
    const result = await api(connectionListPath(current.hostCursor));
    if (panel !== current) return;
    const items = (result.items || []).filter((item) => item.host_connection_id);
    current.hosts = [
      ...new Map(
        [...current.hosts, ...items].map((item) => [item.host_connection_id, item]),
      ).values(),
    ];
    current.hostCursor = result.next_cursor || '';
    box.innerHTML = current.hosts.length
      ? current.hosts
          .map((item) => {
            const last = item.last_round_trip_at || item.verified_at;
            const summary = connectionRoomSummary(item);
            const label = [connectionName(item), summary].filter(Boolean).join(' · ');
            return `<article class="cr-member"><div><h3>${E(connectionName(item))}</h3><p>${E([connectionState(item), summary].filter(Boolean).join(' · '))}</p>${last ? `<small>最近往返 ${E(dateTime(last))}</small>` : ''}</div>${btn('host-detail', '详情', `data-id="${E(item.host_connection_id)}" aria-label="查看 ${E(label)} 的连接详情"`)}</article>`;
          })
          .join('')
      : '<p class="cr-muted">当前没有已登记的独立连接。仍可在房间管理成员。</p>';
    more.hidden = !current.hostCursor;
    more.textContent = '更多连接';
  } catch (error) {
    if (panel === current) {
      panelError(error.message);
      more.hidden = false;
      more.textContent = '重新读取';
    }
  } finally {
    current.loading = false;
    if (panel === current) more.disabled = false;
  }
}

export async function hostDetailPanel(id) {
  if (!id) return;
  const original = rememberedBatch(id);
  openPanel(
    'host-detail',
    'dot 连接详情',
    '<div data-cr-host-summary aria-live="polite"><p class="cr-muted">正在读取连接状态…</p></div>' +
      btn('host-impact', '查看影响范围与连接操作', 'hidden', 'cr-text-button cr-full') +
      (original
        ? btn(
            'view-host-batch',
            '查看最近一次更换',
            `data-id="${E(original)}"`,
            'cr-text-button cr-full',
          )
        : '') +
      btn('host-list', '所有 dot 连接', '', 'cr-text-button cr-full') +
      '<div data-cr-host-impact></div>',
    { hostId: id },
  );
  await refreshHostDetail();
}

export async function refreshHostDetail() {
  const current = panel;
  if (
    current?.name !== 'host-detail' ||
    current.hostLoading ||
    current.impactLoading ||
    current.busy
  )
    return;
  current.hostLoading = true;
  try {
    const result = await api(hostPath(current.hostId));
    if (panel !== current || current.busy) return;
    const host = result.connection || result;
    current.host = host;
    const last = host.last_round_trip_at || host.verified_at;
    const capabilities = host.connection_capabilities || result.connection_capabilities || {};
    const box = root.querySelector('[data-cr-host-summary]');
    const markup = `<section class="cr-connect-summary"><h3>${E(connectionName(host))}</h3>${facts([
      ['连接状态', connectionState(host)],
      ['使用中的房间', host.single_room_title || supplied(host.room_count)],
      ['最近一次往返', last ? dateTime(last) : '等待真实往返'],
    ])}${['wait_for_host_refresh', 'awaiting_host_refresh'].includes(host.next_action) ? '<p>请在原 dot 中继续连接。</p>' : ''}</section>`;
    if (box._markup !== markup) {
      box.innerHTML = markup;
      box._markup = markup;
    }
    const impactButton = root.querySelector('[data-cr-action="host-impact"]');
    impactButton.hidden = host.state === 'revoked' || capabilities.host_revoke !== true;
    if (impactButton.hidden && current.impact) {
      root.querySelector('[data-cr-host-impact]').replaceChildren();
      current.impact = null;
    }
  } finally {
    current.hostLoading = false;
  }
}

export function renderHostImpact(current, more, changed, added, removed) {
  const box = root.querySelector('[data-cr-host-impact]');
  if (!box) return;
  const impact = current.impact;
  const selected = changed
    ? new Set()
    : new Set(
        [...box.querySelectorAll('[data-cr-host-member]:checked')].map((input) => input.value),
      );
  if (!more || !box.querySelector('[data-cr-impact-members]')) {
    const changedNotice = changed
      ? `<p role="status">影响范围已更新。${added.length ? ` 新增 ${E(added.map(hostImpactLabel).join('、'))}。` : ''}${removed.length ? ` 移出 ${E(removed.map(hostImpactLabel).join('、'))}。` : ''}</p>`
      : '';
    const canBatch = current.host.connection_capabilities?.batch_rebind === true;
    box.innerHTML = `<section class="cr-connect-summary" aria-label="受影响的房间"><h3>受影响的房间</h3>${changedNotice}${facts(
      [
        ['房间', supplied(impact.room_count)],
        ['排队工作', supplied(impact.queued_work_count)],
        ['已派发操作', supplied(impact.dispatched_operation_count)],
      ],
    )}<p class="cr-muted">撤销这条连接会影响此处列出的房间。同名的其他连接继续使用。已派发的操作可能仍在结束，原结果会继续核对。</p>${canBatch ? '<p class="cr-muted">勾选成员可单独发起批量更换。</p>' : ''}<ul class="cr-host-members" data-cr-impact-members></ul>${btn('more-host-impact', '更多受影响房间', 'data-cr-more-impact hidden', 'cr-text-button')}${canBatch ? `<div data-cr-batch-target hidden>${field('更换为', '<select name="batch_host_connection"><option value="">分别验证新连接</option></select>')}<p class="cr-muted" data-cr-batch-target-status aria-live="polite"></p>${btn('load-batch-targets', '查看已有 dot', 'data-cr-more-targets', 'cr-text-button')}</div>${btn('start-host-batch', '为所选成员更换连接', 'data-cr-start-batch disabled', 'cr-text-button')}` : ''}${btn('revoke-host', '撤销这条连接', 'data-cr-revoke-host', 'cr-danger cr-full')}</section>`;
  }
  const membersBox = box.querySelector('[data-cr-impact-members]');
  const displayed = new Set([...membersBox.children].map((row) => row.dataset.binding));
  const start = box.querySelector('[data-cr-start-batch]');
  for (const item of current.impactMembers) {
    const binding = hostImpactBinding(item);
    if (displayed.has(binding)) continue;
    const selectable =
      !!start &&
      !item.candidate &&
      Number.isInteger(item.binding_generation ?? item.expected_generation);
    membersBox.insertAdjacentHTML(
      'beforeend',
      `<li data-binding="${E(binding)}">${selectable ? `<label><input type="checkbox" data-cr-host-member value="${E(item.member_id)}" ${selected.has(item.member_id) ? 'checked' : ''}>${E(hostImpactLabel(item))}</label>` : E(hostImpactLabel(item)) + (item.candidate ? ' · 待验证的新连接' : '')}</li>`,
    );
  }
  updateHostBatchControls();
  if (!more && current.batchTargets?.length) renderHostBatchTargets(current);
  box.querySelector('[data-cr-more-impact]').hidden = !current.impactCursor;
  box.querySelector('[data-cr-revoke-host]').disabled =
    !impact.impact_digest || !Number.isInteger(impact.authority_version);
}

export async function loadHostImpact(more = false) {
  const current = panel;
  if (current?.name !== 'host-detail' || !current.host || current.impactLoading) return;
  current.impactLoading = true;
  const box = root.querySelector('[data-cr-host-impact]');
  const trigger = root.querySelector(
    more ? '[data-cr-more-impact]' : '[data-cr-action="host-impact"]',
  );
  if (trigger) trigger.disabled = true;
  if (!current.impact) box.textContent = '正在核对受影响的房间…';
  try {
    const result = await api(
      hostPath(current.hostId) +
        '/impact?limit=40' +
        (more && current.impactCursor ? '&cursor=' + encodeURIComponent(current.impactCursor) : ''),
    );
    if (panel !== current) return;
    const impact = result.impact || result;
    const items = impact.items || result.items || [];
    const impactDigest = impact.impact_digest || impact.digest;
    const oldItems = current.impactMembers || [];
    const changed = current.impact && current.impact.impact_digest !== impactDigest;
    if (more && changed) {
      current.impactLoading = false;
      await loadHostImpact();
      return;
    }
    const added = changed
      ? items.filter(
          (item) => !oldItems.some((old) => hostImpactBinding(old) === hostImpactBinding(item)),
        )
      : [];
    const removed =
      changed && !impact.next_cursor
        ? oldItems.filter(
            (item) => !items.some((next) => hostImpactBinding(next) === hostImpactBinding(item)),
          )
        : [];
    current.impact = { ...impact, impact_digest: impactDigest };
    current.impactCursor = impact.next_cursor || result.next_cursor || '';
    current.impactMembers = [
      ...new Map(
        [...(more ? oldItems : []), ...items].map((item) => [hostImpactBinding(item), item]),
      ).values(),
    ];
    renderHostImpact(current, more, changed, added, removed);
    if (changed) panelError('连接影响范围已变化，请核对当前房间列表后再确认。');
    if (!more) box.querySelector('h3')?.scrollIntoView({ block: 'nearest' });
  } catch (error) {
    if (panel === current) {
      if (more && error.code === 'INVALID_CURSOR') {
        current.impactLoading = false;
        await loadHostImpact();
        return;
      }
      if (!current.impact) box.textContent = '';
      panelError(error.message);
    }
  } finally {
    current.impactLoading = false;
    if (panel === current && trigger?.isConnected) trigger.disabled = false;
  }
}

export async function revokeHost() {
  const current = panel;
  const impact = current?.impact;
  if (!impact?.impact_digest || !Number.isInteger(impact.authority_version)) return;
  try {
    const result = await mutation(
      'POST',
      hostPath(current.hostId) + '/revoke',
      {
        expected_authority_version: impact.authority_version,
        impact_digest: impact.impact_digest,
      },
      current,
    );
    if (result) {
      await hostDetailPanel(current.hostId);
      await store.refresh();
      announce('这条连接已撤销。已派发操作会继续核对原结果。');
    }
  } catch (error) {
    if (panel === current && error.code === 'ROOM_CONNECTION_IMPACT_CHANGED') {
      await loadHostImpact();
      return;
    }
    throw error;
  }
}

export function rememberedBatch(hostId, batchId) {
  try {
    const key = ['codepier-host-batch', state.owner, hostId].join(':');
    if (batchId) sessionStorage.setItem(key, batchId);
    return sessionStorage.getItem(key);
  } catch {
    return '';
  }
}

export function updateHostBatchControls() {
  const start = root.querySelector('[data-cr-start-batch]');
  const target = root.querySelector('[data-cr-batch-target]');
  if (!start || !target) return;
  const checked = !!root.querySelector('[data-cr-host-member]:checked');
  start.disabled = !checked;
  target.hidden = !checked;
}

export function renderHostBatchTargets(current) {
  const select = root.querySelector('[name="batch_host_connection"]');
  const selected = select.value;
  select.innerHTML =
    '<option value="">分别验证新连接</option>' +
    current.batchTargets
      .map(
        (item) =>
          `<option value="${E(item.host_connection_id)}">${E(connectionChoiceLabel(item))}</option>`,
      )
      .join('');
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
  const button = root.querySelector('[data-cr-more-targets]');
  button.hidden = !current.batchTargetCursor;
  button.textContent = '更多已登记的 dot';
  root.querySelector('[data-cr-batch-target-status]').textContent = current.batchTargets.length
    ? ''
    : current.batchTargetCursor
      ? '这一页没有可用连接。'
      : '当前没有其他可复用的 dot。';
}

export async function loadHostBatchTargets() {
  const current = panel;
  if (current?.name !== 'host-detail' || current.batchTargetsLoading) return;
  current.batchTargetsLoading = true;
  const status = root.querySelector('[data-cr-batch-target-status]');
  const button = root.querySelector('[data-cr-more-targets]');
  status.textContent = '正在读取已有 dot…';
  button.disabled = true;
  try {
    const response = await api(connectionListPath(current.batchTargetCursor));
    if (panel !== current || !button.isConnected) return;
    const items = (response.items || []).filter(
      (item) =>
        item.host_connection_id &&
        item.host_connection_id !== current.hostId &&
        supportsHostReuse(item, response.connection_capabilities || {}),
    );
    current.batchTargets = [
      ...new Map(
        [...(current.batchTargets || []), ...items].map((item) => [item.host_connection_id, item]),
      ).values(),
    ];
    current.batchTargetCursor = response.next_cursor || '';
    renderHostBatchTargets(current);
  } catch (error) {
    if (panel === current && button.isConnected) {
      status.textContent = error.message;
      button.textContent = '重新读取已有 dot';
    }
  } finally {
    current.batchTargetsLoading = false;
    if (panel === current && button.isConnected) button.disabled = false;
  }
}

export async function startHostBatch() {
  const current = panel;
  if (current?.name !== 'host-detail') return;
  const checked = new Set(
    [...root.querySelectorAll('[data-cr-host-member]:checked')].map((input) => input.value),
  );
  const selected = current.impactMembers
    .filter((item) => !item.candidate && checked.has(item.member_id))
    .map((item) => ({
      member_id: item.member_id,
      expected_generation: item.binding_generation ?? item.expected_generation,
    }));
  if (!selected.length) return;
  const hostId = root.querySelector('[name="batch_host_connection"]')?.value;
  const result = await mutation(
    'POST',
    batchPath,
    { members: selected, ...(hostId ? { host_connection_id: hostId } : {}) },
    current,
  );
  if (result?.batch_id) {
    rememberedBatch(current.hostId, result.batch_id);
    await hostBatchPanel(result.batch_id, current.hostId, result);
  }
}

export async function hostBatchPanel(batchId, hostId = '', result = null) {
  if (!batchId) return;
  openPanel(
    'host-batch',
    '批量更换连接',
    '<p class="cr-muted">各房间会分别验证新连接，原连接在验证通过后切换。</p>' +
      '<div data-cr-batch-results aria-live="polite"></div>' +
      btn('more-host-batch', '更多结果', 'data-cr-more-batch hidden', 'cr-text-button') +
      (hostId
        ? btn('host-detail', '返回连接详情', `data-id="${E(hostId)}"`, 'cr-text-button cr-full')
        : ''),
    { batchId, batchItems: [], batchCursor: '' },
  );
  if (result) renderHostBatch(panel, result, false);
  else await refreshHostBatch();
}

export function renderHostBatch(current, result, more) {
  current.batchCursor = result.next_cursor || '';
  current.batchComplete = result.complete === true;
  current.batchItems = [
    ...new Map(
      [...(more ? current.batchItems : []), ...(result.items || [])].map((item) => [
        item.member_id,
        item,
      ]),
    ).values(),
  ];
  const box = root.querySelector('[data-cr-batch-results]');
  const markup = `<ul class="cr-host-members">${current.batchItems
    .map((item) => {
      const label = hostBatchLabels[item.status] || '正在读取状态';
      const message = item.message || hostBatchErrors[item.error_code];
      const canOpen = item.conversation_id && item.member_id && item.flow_id;
      return `<li><span>${E(hostImpactLabel(item))} · ${E(label)}${message ? ` · ${E(message)}` : ''}</span>${canOpen ? btn('open-batch-member', '查看成员', `data-id="${E(item.member_id)}" data-room-id="${E(item.conversation_id)}"`, 'cr-text-button') : ''}</li>`;
    })
    .join('')}</ul>`;
  if (box._markup !== markup) {
    const focused = box.contains(document.activeElement) ? document.activeElement.dataset.id : '';
    box.innerHTML = markup;
    box._markup = markup;
    if (focused)
      [...box.querySelectorAll('button')]
        .find((button) => button.dataset.id === focused)
        ?.focus({ preventScroll: true });
  }
  root.querySelector('[data-cr-more-batch]').hidden = !current.batchCursor;
}

export async function refreshHostBatch(more = false) {
  const current = panel;
  if (current?.name !== 'host-batch' || current.batchLoading) return;
  current.batchLoading = true;
  try {
    // Reload from the beginning after a hint so statuses are reconciled in
    // order, even when the owner has already viewed additional pages.
    let cursor = more ? current.batchCursor : '';
    const items = [];
    let result;
    do {
      result = await api(
        batchPath +
          '/' +
          encodeURIComponent(current.batchId) +
          '?limit=40' +
          (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''),
      );
      if (panel !== current) return;
      items.push(...(result.items || []));
      cursor = result.next_cursor;
    } while (!more && cursor && items.length < current.batchItems.length);
    renderHostBatch(current, { ...result, items }, more);
  } finally {
    current.batchLoading = false;
  }
}

export async function returnToChannel(current) {
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

export async function memberPanel(id) {
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

export function instructions(value) {
  if (!value) return '';
  return typeof value === 'string' ? value : value.text || value.prompt || '';
}

export function connectionRenewalMarkup(renewal, member, pendingBinding) {
  if (!renewal || member?.host_connection_state === 'revoked') return '';
  const managed = renewal.approved === true && renewal.mode === 'managed';
  const status = {
    stopped: renewal.reason_code === 'authorization_expired' ? '授权已到期' : '自动恢复已停止',
    awaiting_probe: '等待连接验证',
    waiting: '等待 dot 恢复',
    recovering: '正在恢复',
    expired: '等待宿主续订',
    awaiting_host_refresh: '等待宿主续订',
  }[renewal.state];
  const control =
    activeRoom() && member && !member.legacy && member.state !== 'removed' && !pendingBinding
      ? btn(
          'connection-renewal',
          managed ? '关闭自动恢复' : '开启自动恢复',
          `data-renewal-enable="${!managed}"`,
          'cr-text-button',
        )
      : '';
  return `<section class="cr-connect-summary" aria-label="连接保持方式"><strong>由 dot 宿主续订</strong>${status ? `<p class="cr-muted">${E(status)}</p>` : ''}${facts(
    [
      ['自动恢复', managed ? '已开启' : '已关闭'],
      ['恢复授权至', managed && renewal.approved_until ? dateTime(renewal.approved_until) : null],
      ['当前订阅到期', renewal.expires_at ? dateTime(renewal.expires_at) : null],
    ],
  )}${control}</section>`;
}

export function renderFlow(result) {
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
    flow.host_connection_state === 'waiting_host_renewal' &&
    flow.display?.primary_action === 'continue_connection'
      ? 'wait_for_host_refresh'
      : flow.display?.primary_action || flow.primary_action || member?.display?.primary_action;
  const stage = flow.stage || flow.state || member?.state;
  const ready = ['ready_chat', 'ready_work'].includes(stage);
  if (ready || flow.awaiting_host_control) panel.joinInstructions = '';
  const renewal = flow.subscription_renewal || member?.subscription_renewal;
  const renewalMarkup = connectionRenewalMarkup(renewal, member, flow.pending_binding);
  const markup = `<div class="cr-flow-state" data-stage="${E(stage)}"><span class="cr-flow-symbol ${ready ? 'is-ready' : ''}" aria-hidden="true">${ready ? '✓' : icon('network')}</span><h3>${E(flow.display?.title || title(flow))}</h3>${flow.display?.reason ? `<p>${E(flow.display.reason)}</p>` : ''}</div>${renewalMarkup}${flow.pending_binding ? '<p class="cr-info">新连接验证通过后才会替换当前连接。原成员和工作保持原来的身份。</p>' : !ready && (member?.host_connection_id || member?.connection_source || member?.connection_source_member_id) ? '<p class="cr-info">请在所选 dot 中完成本房间的接入验证。此房间的参与范围单独生效。</p>' : ''}${panel.joinInstructions ? `<label class="cr-field"><span>加入说明</span><textarea class="cr-instructions" readonly rows="7" aria-label="dot 加入说明"></textarea></label>${btn('copy-instructions', '复制加入说明', '', 'cr-primary cr-full')}` : ''}${primary && !['copy_instructions', 'copy_join_instructions', 'send_message', 'start_work', 'none'].includes(primary) ? btn('flow-action', actionLabel(primary), `data-flow-action="${E(primary)}"`, 'cr-primary cr-full') : ''}${member ? `<section class="cr-member-controls">${btn('permissions', '权限设置')}${member.host_connection_id ? btn('host-detail', '管理这条连接', `data-id="${E(member.host_connection_id)}"`) : ''}${member.legacy ? btn('flow-action', '迁移此连接', 'data-flow-action="migrate"') : member.state !== 'removed' && primary !== 'rebind' ? btn('rebind-member', flow.pending_binding ? '查看更换说明' : '更换连接') : ''}${state.room?.lead_member_id !== memberId(member) && member.state !== 'removed' ? btn('lead', '设为主责', `data-id="${E(memberId(member))}"`) : ''}${member.state !== 'removed' && member.state !== 'paused' ? btn('flow-action', '暂停此成员', 'data-flow-action="pause"') : ''}${member.state !== 'removed' ? btn('remove-member', '移除成员', '', 'cr-danger') : ''}</section>` : ''}<details class="cr-diagnostics"><summary>连接详情</summary><dl>${Object.entries(
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

export async function refreshFlow() {
  if (!panel?.flowId || panel.busy) return;
  const current = panel,
    epoch = panelEpoch,
    writeEpoch = current.writeEpoch || 0;
  const response = await api(base + '/connect-flows/' + encodeURIComponent(current.flowId));
  if (panel === current && epoch === panelEpoch && writeEpoch === (current.writeEpoch || 0))
    renderFlow(response);
}
