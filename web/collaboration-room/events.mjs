// Events: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  addMemberPanel,
  announce,
  applyDeliveryControl,
  base,
  btn,
  closePanel,
  contextSource,
  controlWork,
  createPanel,
  deliveryControlKey,
  deliveryControlPreviews,
  hostBatchPanel,
  hostDetailPanel,
  hostListPanel,
  identifier,
  legacyChannelRemediation,
  loadConnections,
  loadContext,
  loadDeliveryRelationships,
  loadDiagnostics,
  loadHostBatchTargets,
  loadHostImpact,
  loadHostList,
  loadWakeEvidence,
  memberPanel,
  membersPanel,
  migrationPanel,
  mutation,
  openPanel,
  paintDeliveryControl,
  panel,
  panelError,
  patchComposer,
  permissionsPanel,
  previewContinuation,
  previewDeliveryControl,
  readOnlyReason,
  readingSnapshot,
  recoverAction,
  recoverSend,
  refreshHostBatch,
  remember,
  rememberReading,
  renderChannel,
  renderContext,
  renderFlow,
  renderWorkList,
  restoreAnchor,
  returnToChannel,
  revokeHost,
  root,
  selectRoom,
  settingsPanel,
  startHostBatch,
  startWeixin,
  state,
  store,
  weixinPanel,
  workPanel,
  workSourceId,
} from './index.mjs';

export async function click(button) {
  const action = button.dataset.crAction,
    id = button.dataset.id;
  if (
    [
      'reply',
      'reply-work',
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
  else if (action === 'host-list') await hostListPanel();
  else if (action === 'more-hosts') await loadHostList();
  else if (action === 'host-detail') await hostDetailPanel(id);
  else if (action === 'host-impact' || action === 'more-host-impact')
    await loadHostImpact(action === 'more-host-impact');
  else if (action === 'revoke-host') await revokeHost();
  else if (action === 'load-batch-targets') await loadHostBatchTargets();
  else if (action === 'start-host-batch') await startHostBatch();
  else if (action === 'view-host-batch') await hostBatchPanel(id, panel.hostId);
  else if (action === 'more-host-batch') await refreshHostBatch(true);
  else if (action === 'open-batch-member') {
    const targetRoomId = button.dataset.roomId;
    if (state.roomId !== targetRoomId) await selectRoom(targetRoomId);
    if (state.roomId === targetRoomId) await memberPanel(id);
  } else if (action === 'member-detail') await memberPanel(id);
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
  } else if (action === 'preview-delivery-control') {
    await previewDeliveryControl(button, {
      action: button.dataset.control,
      ...(button.dataset.part ? { part_ids: [button.dataset.part] } : {}),
    });
  } else if (action === 'apply-delivery-control') await applyDeliveryControl(button);
  else if (action === 'dismiss-delivery-control') {
    deliveryControlPreviews.delete(deliveryControlKey(id));
    paintDeliveryControl(button, id);
  } else if (action === 'delivery-relationships') await loadDeliveryRelationships(button);
  else if (action === 'retry-delivery') {
    const result = await mutation(
      'POST',
      store.path() + '/weixin-deliveries/' + encodeURIComponent(id) + '/retry',
      {
        expected_version: Number(button.dataset.version),
        ...(button.dataset.part
          ? {
              part_ids: [button.dataset.part],
              expected_ordering_revision: Number(button.dataset.ordering),
            }
          : {}),
      },
    );
    if (result) {
      await store.messages();
      announce('已重新排队发送');
    }
  } else if (action === 'copy-instructions') {
    await navigator.clipboard.writeText(panel.joinInstructions);
    button.textContent = '已复制';
  } else if (action === 'reply' || action === 'reply-work') {
    const source = action === 'reply-work' ? workSourceId(panel.work) : id;
    if (!source) return;
    Object.assign(store.draft(), {
      reply: source,
      work: '',
      workVersion: 0,
      contextCorrection: null,
    });
    if (action === 'reply-work') closePanel({ restoreFocus: false });
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
    if (hadFocus && (document.activeElement === button || document.activeElement === document.body))
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
        wait_for_host_refresh: 'resume',
        awaiting_host_refresh: 'resume',
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
    store.draft().reply = '';
    store.draft().contextCorrection = null;
    closePanel({ restoreFocus: false });
    patchComposer();
    root.querySelector('#cr-message').focus();
    announce('下一条消息将更新原交办');
  } else if (action === 'diagnostics') {
    openPanel(
      'diagnostics',
      '运行诊断',
      '<div data-cr-diagnostics-view><p class="cr-muted" data-cr-diagnostics-loading role="status">正在读取…</p></div>',
    );
    await loadDiagnostics();
  } else if (action === 'refresh-diagnostics') await loadDiagnostics();
  else if (action === 'wake-evidence') await loadWakeEvidence(button);
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
