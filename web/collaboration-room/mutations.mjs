// Mutations: room UI behavior; lifecycle state is owned by index.mjs.
import {
  announce,
  base,
  batchPath,
  connectionsPath,
  panel,
  panelEpoch,
  panelError,
  patchPending,
  patchRoomPermissions,
  recoverAction,
  renderMigration,
  renderWork,
  requireMutation,
  root,
  state,
  store,
} from './index.mjs';

export async function mutation(method, url, body, current = panel, options = {}) {
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
          : url === batchPath
            ? 'host-batch'
            : url.startsWith(connectionsPath + '/') && url.endsWith('/revoke')
              ? 'host-revoke'
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
    hostId: current?.hostId,
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
      'host-batch': '批量更换 dot 连接',
      'host-revoke': '撤销 dot 连接',
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
      for (const button of form.querySelectorAll('button[type="submit"]')) button.disabled = false;
    }
    for (const button of actionButtons) if (button.isConnected) button.disabled = false;
    patchRoomPermissions();
  }
}
