// Timeline: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  announce,
  btn,
  dateTime,
  deliveryControlPreviews,
  deliveryLabels,
  diagnosticFields,
  finishedWork,
  identifier,
  mutation,
  mutationAllowed,
  panel,
  panelError,
  patchComposer,
  patchEmpty,
  patchHeader,
  patchRoomPermissions,
  readingSnapshot,
  reasonText,
  renderContext,
  renderWork,
  restoreAnchor,
  root,
  selection,
  sizeEditor,
  state,
  store,
  supplied,
  time,
  workSourceId,
  workStatus,
} from './index.mjs';

export function fileSize(value) {
  return value >= 1024 * 1024
    ? (value / (1024 * 1024)).toFixed(1) + ' MiB'
    : value >= 1024
      ? Math.ceil(value / 1024) + ' KiB'
      : value + ' B';
}

export function patchFiles() {
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

export function attachmentMarkup(item) {
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

export function messageProgress(message) {
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

export function messageMarkup(message) {
  const owner = ['owner', 'user', 'human'].includes(message.author_kind);
  return `<header><strong>${E(message.author_name || message.display_name || message.member_name || (owner ? '你' : message.author_kind === 'system' ? '房间动态' : 'dot'))}</strong><time>${E(time(message.created_at || message.created))}</time></header>${message.reply_to_id ? '<span class="cr-message-reference">回复原消息</span>' : ''}<div class="cr-message-body" ${message.body_text ? '' : 'hidden'}></div>${message.attachments?.length ? `<div class="cr-message-files">${message.attachments.map(attachmentMarkup).join('')}</div>` : ''}${deliveryMarkup(message.channel_deliveries)}<div class="cr-source-works" data-cr-source-works hidden></div><footer>${messageProgress(message)}${btn('reply', '回复', `data-id="${E(message.id)}"`, 'cr-text-button')}</footer>`;
}

export function deliveryReason(code) {
  return (
    {
      WEIXIN_RATE_LIMITED: '微信暂时限制发送',
      WEIXIN_REQUEST_LIMIT: '消息或附件超出微信发送上限',
      attachment_unavailable: '原附件暂不可用',
      daily_reply_quota: '已达到今日回传额度',
      context_missing: '等待原微信私聊的会话上下文',
      WEIXIN_RECOVERY_SCOPE_CHANGED: '原内容、收件人或发送权限发生变化',
      dependency_waiting: '等待前置回复发送完成',
      dependency_failed: '前置回复未能发送',
      source_unavailable: '原消息或微信连接已失效',
      provider_response_unknown: '微信未返回可核对的发送结果',
      authorization_revoked: '工作许可已撤销',
      authorization_expired: '工作许可已到期',
      scope_changed: '原工作范围已变化',
      historical_ordering_unverified: '无法确认旧回复的顺序关系，暂时保留顺序等待',
    }[code] || reasonText(code)
  );
}

export function deliveryControlKey(id) {
  return [state.owner, state.roomId, id].join(':');
}

export function deliveryControlPreviewMarkup(item) {
  if (!item) return '';
  const { preview, selection } = item;
  const confirming = selection.action === 'confirm_received';
  const label = confirming ? '确认已收到所选分项' : '停止这些分项';
  const attr = `data-id="${E(preview.delivery_id)}"`;
  return `<p>${confirming ? '核对所选分项' : '将停止尚未发送的内容'} · ${E(preview.affected_count)} 段</p>${
    preview.affected_count
      ? `<ul class="cr-delivery-parts">${preview.affected_parts
          .map(
            (part) =>
              `<li>${part.delivery_id === preview.delivery_id ? '原回复' : '关联回复'} · 第 ${E(Number(part.ordinal) + 1)} 段 · ${E(deliveryLabels[part.state] || part.state)}</li>`,
          )
          .join('')}</ul>${btn('apply-delivery-control', label, attr, 'cr-text-button')}`
      : '<p class="cr-muted">当前没有符合条件的分项。</p>'
  }${btn('dismiss-delivery-control', '关闭核对', attr, 'cr-text-button')}`;
}

export function deliveryPath(id) {
  return store.path() + '/weixin-deliveries/' + encodeURIComponent(id);
}

export function paintDeliveryControl(button, id) {
  const item = deliveryControlPreviews.get(deliveryControlKey(id));
  const box = button
    .closest('[data-delivery-id]')
    ?.querySelector('[data-cr-delivery-control-preview]');
  if (!box) return;
  box.innerHTML = deliveryControlPreviewMarkup(item);
  box.querySelector('button')?.focus({ preventScroll: true });
}

export async function previewDeliveryControl(button, selection) {
  const id = button.dataset.id;
  const key = deliveryControlKey(id);
  button.disabled = true;
  try {
    const preview = await api(deliveryPath(id) + '/control-preview', {
      method: 'POST',
      body: JSON.stringify(selection),
    });
    if (key !== deliveryControlKey(id)) return;
    deliveryControlPreviews.set(key, { preview, selection, requestKey: store.key() });
    paintDeliveryControl(button, id);
  } finally {
    button.disabled = false;
  }
}

export async function applyDeliveryControl(button) {
  const id = button.dataset.id;
  const key = deliveryControlKey(id);
  const item = deliveryControlPreviews.get(key);
  if (!item) return;
  const { selection, preview, requestKey } = item;
  button.disabled = true;
  try {
    const result = await mutation('POST', deliveryPath(id) + '/control', {
      ...selection,
      expected_version: preview.delivery_version,
      expected_ordering_revision: preview.ordering_revision,
      preview_digest: preview.preview_digest,
      confirmation_note:
        selection.action === 'confirm_received' ? '本人核对手机微信收到所选分项' : '',
      idempotency_key: requestKey,
    });
    if (!result || key !== deliveryControlKey(id)) return;
    deliveryControlPreviews.delete(key);
    paintDeliveryControl(button, id);
    await store.messages();
    announce(
      selection.action === 'confirm_received'
        ? '已保存所选分项的核对结果'
        : '已停止所选范围内未发送的内容',
    );
  } catch (error) {
    if (error.status !== 409 || key !== deliveryControlKey(id)) throw error;
    await previewDeliveryControl(button, selection);
    announce('发送状态刚有变化，请核对更新后的范围。');
  } finally {
    button.disabled = false;
  }
}

export async function loadDeliveryRelationships(button) {
  const id = button.dataset.id;
  const roomId = state.roomId;
  const target = button.closest('[data-cr-delivery-relationships]');
  const data = await api(
    store.path(roomId) + '/diagnostics?' + new URLSearchParams({ delivery_id: id }),
  );
  if (state.roomId !== roomId || !target?.isConnected) return;
  const prior = data.direct_blocker;
  const count = (value) => (data.truncated ? '至少 ' : '') + supplied(value);
  target.innerHTML = diagnosticFields([
    [
      '最近前置投递',
      prior?.restricted
        ? '当前授权下不可查看'
        : prior?.delivery_id
          ? identifier(prior.delivery_id)
          : '无',
    ],
    ['关系', prior?.reason_code ? deliveryReason(prior.reason_code) : '—'],
    ['直接等待', count(data.direct_waiting_count)],
    ['受影响的后续回复', count(data.affected_waiting_count)],
  ]);
}

export function deliveryMarkup(deliveries = [], display, truncated = false) {
  const visible = (deliveries || []).filter((item) => item.state !== 'not_requested');
  if (!visible.length) return '';
  return `<section class="cr-deliveries" aria-label="发送状态" data-cr-deliveries>${visible
    .map((delivery) => {
      const parts = delivery.parts || [];
      const accepted = parts.filter((part) => part.state === 'accepted').length;
      const confirmed = parts.filter((part) => part.state === 'owner_confirmed').length;
      const unknown = parts.filter((part) => part.state === 'unknown').length;
      const stopped = parts.filter((part) =>
        ['cancelled', 'rejected', 'superseded'].includes(part.state),
      ).length;
      const name = delivery.channel === 'weixin' ? '微信' : delivery.channel || '渠道';
      let label = name + ' · ' + (deliveryLabels[delivery.state] || '状态待核对');
      if (delivery.state === 'blocked' && !delivery.id) label = name + '回复待处理';
      if (delivery.state === 'rejected' && !delivery.id) label = name + '回复未发送';
      if (unknown || delivery.state === 'unknown')
        label = accepted
          ? `${name}接口已受理 ${accepted} 段，${unknown} 段待核对`
          : name + '发送结果待核对';
      else if (stopped && (accepted || confirmed))
        label = `${name} · ${accepted ? '已受理 ' + accepted + ' 段，' : ''}${confirmed ? '本人核对 ' + confirmed + ' 段，' : ''}${stopped} 段已停止`;
      else if (confirmed)
        label = `${name} · ${accepted ? '已受理 ' + accepted + ' 段，' : ''}本人已核对收到 ${confirmed} 段`;
      else if (delivery.state === 'accepted') label = name + '接口已受理';
      if (delivery.read_verified === true || delivery.provider_delivery_verified === true)
        label = name + '已送达';
      const controllable =
        activeRoom() &&
        delivery.channel === 'weixin' &&
        !!delivery.id &&
        Number.isInteger(delivery.version) &&
        Number.isInteger(delivery.ordering_revision);
      const stop =
        controllable &&
        parts.some(
          (part) =>
            ['queued', 'preparing', 'waiting_context', 'waiting_media'].includes(part.state) ||
            (part.state === 'attention' &&
              (part.attempts === 0 || part.reason_code === 'WEIXIN_RATE_LIMITED')),
        );
      if (
        !unknown &&
        parts.some((part) => part.state === 'queued' && part.reason_code === 'WEIXIN_RATE_LIMITED')
      )
        label = `平台限流，${delivery.next_attempt_at ? dateTime(delivery.next_attempt_at) + ' 后继续发送' : '稍后自动继续'}`;
      const retry =
        activeRoom() &&
        delivery.channel === 'weixin' &&
        !!delivery.id &&
        Number.isInteger(delivery.version) &&
        parts.some((part) => part.state === 'attention' && part.attempts === 0);
      const detail =
        unknown ||
        confirmed ||
        stopped ||
        retry ||
        stop ||
        !!delivery.ordering_reason ||
        !!delivery.reason_code ||
        ['unknown', 'needs_attention', 'blocked'].includes(delivery.state);
      const attr = `data-id="${E(delivery.id)}"`;
      return `<div class="cr-delivery" data-delivery-id="${E(delivery.id)}" data-state="${E(unknown ? 'unknown' : delivery.state)}"><span class="cr-delivery-title">${E(label)}${delivery.kind === 'status' ? ' · 状态通知' : ''}</span>${
        detail
          ? `<details><summary>查看原回复和分项</summary>
      ${delivery.display?.reason ? `<p>${E(delivery.display.reason)}</p>` : ''}
      ${delivery.ordering_reason || delivery.reason_code ? `<p>${E(deliveryReason(delivery.ordering_reason || delivery.reason_code))}</p>` : ''}
      ${parts.length ? `<ul class="cr-delivery-parts">${parts.map((part) => `<li data-delivery-part="${E(part.id || part.ordinal)}" data-state="${E(part.state)}">第 ${E(Number(part.ordinal) + 1)} 段 · ${E({ text: '文字', image: '图片', file: '文件', voice: '语音', video: '视频' }[part.kind] || '消息')} · ${E(deliveryLabels[part.state] || '状态待核对')}${part.reason_code && part.state !== 'owner_confirmed' ? `<p>${E(deliveryReason(part.reason_code))}</p>` : ''}${controllable && part.id && part.state === 'unknown' ? btn('preview-delivery-control', '核对这一段', `${attr} data-control="confirm_received" data-part="${E(part.id)}"`, 'cr-text-button') : ''}${controllable && part.route_recovery_blocked ? `<p>${E(deliveryReason(part.route_recovery_blocked))}</p>` : ''}${controllable && part.id && part.route_recovery_available === true ? btn('retry-delivery', '恢复发送此段', `${attr} data-version="${E(delivery.version)}" data-ordering="${E(delivery.ordering_revision)}" data-part="${E(part.id)}"`, 'cr-text-button') : ''}</li>`).join('')}</ul>` : ''}
      ${retry ? btn('retry-delivery', '重试可发送分项', `${attr} data-version="${E(delivery.version)}"`, 'cr-text-button') : ''}
      ${stop ? btn('preview-delivery-control', '停止尚未发送的后续内容', `${attr} data-control="cancel_unsent"`, 'cr-text-button') : ''}
      <div data-cr-delivery-control-preview aria-live="polite">${deliveryControlPreviewMarkup(deliveryControlPreviews.get(deliveryControlKey(delivery.id)))}</div>
      ${delivery.channel === 'weixin' && delivery.id ? `<div data-cr-delivery-relationships>${btn('delivery-relationships', '查看关联等待', attr, 'cr-text-button')}</div>` : ''}
      </details>`
          : ''
      }</div>`;
    })
    .join(
      '',
    )}${truncated && display?.reason ? `<p class="cr-muted">${E(display.reason)}</p>` : ''}</section>`;
}

export function patchWindowHints() {
  if (!root) return;
  root.querySelector('[data-cr-older]').hidden = !state.cursor;
  const boundary = root.querySelector('[data-cr-history-end]');
  boundary.hidden = !state.messageHistory;
  boundary.style.minHeight = state.messageHistory
    ? root.querySelector('.cr-feed').clientHeight + 'px'
    : '';
  if (state.newerMessages) root.querySelector('[data-cr-latest]').hidden = false;
}

export function patchMessageContent(node, message) {
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

export function patchMessages() {
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
        (file) => !!(file.retention?.expires_at && file.retention.expires_at * 1000 <= Date.now()),
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

export function patchPending() {
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

export function mutationMarkup() {
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

export function patchMutationRecovery() {
  const box = root?.querySelector('[data-cr-panel-recovery]');
  if (!box) return;
  const markup = mutationMarkup();
  box.hidden = !markup;
  if (box._markup !== markup) {
    box.innerHTML = markup;
    box._markup = markup;
  }
}

export function patchWorks() {
  const node = root?.querySelector('[data-cr-work]');
  if (!node) return;
  const activeWorks = state.works.filter((work) => !finishedWork(work));
  const lead = activeWorks[0];
  const markup = lead
    ? `<span class="cr-work-bar-summary"><span class="cr-work-indicator ${E(lead.state)}" aria-hidden="true"></span><strong>${E(lead.title || '进行中的工作')}</strong><small>${E(workStatus(lead))}${activeWorks.length > 1 ? ` · 共 ${activeWorks.length} 项` : ''}</small></span><span>${btn('work-detail', '查看', `data-id="${E(lead.id)}"`, 'cr-text-button')}${btn('work-list', '全部', '', 'cr-text-button')}</span>`
    : `<span class="cr-work-bar-summary">${state.works.length} 项历史工作</span>${btn('work-list', '工作记录', '', 'cr-text-button')}`;
  node.hidden = !state.works.length && state.worksComplete && !state.worksCursor;
  if (node._markup !== markup) {
    node.innerHTML = markup;
    node._markup = markup;
  }
  if (panel?.name === 'work-list') renderWorkList();
  const bySource = new Map();
  for (const work of state.works) {
    const sourceId = workSourceId(work);
    if (!sourceId) continue;
    if (!bySource.has(sourceId)) bySource.set(sourceId, []);
    bySource.get(sourceId).push(work);
  }
  for (const message of root.querySelectorAll('[data-cr-source-works]')) {
    const works = bySource.get(message.closest('[data-message-id]')?.dataset.messageId) || [];
    const items = works
      .map(
        (work) =>
          `<button type="button" class="cr-timeline-work" data-cr-action="work-detail" data-id="${E(work.id)}" data-work-state="${E(work.state)}"><span class="cr-work-indicator ${E(work.state)}" aria-hidden="true"></span><span class="cr-timeline-work-title">${E(work.title || '工作')}</span><small>${E(workStatus(work))}</small></button>`,
      )
      .join('');
    if (message._markup !== items) {
      const focusedId = message.contains(document.activeElement)
        ? document.activeElement.dataset.id
        : '';
      message.innerHTML = items;
      message._markup = items;
      if (focusedId)
        [...message.querySelectorAll('button')]
          .find((button) => button.dataset.id === focusedId)
          ?.focus({ preventScroll: true });
    }
    message.hidden = !items;
  }
  patchEmpty();
}

export function renderWorkList() {
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
    <p>${E(workStatus(work))}${work.display?.reason ? ' · ' + E(work.display.reason) : ''}</p></div>
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

export function update(domain) {
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
