// Diagnostics: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  attachmentMarkup,
  btn,
  contextKinds,
  dateTime,
  deliveryLabels,
  deliveryMarkup,
  field,
  identifier,
  openPanel,
  panel,
  panelError,
  root,
  stages,
  state,
  store,
  supplied,
  time,
  title,
  wakeEvidenceLabels,
  workLabels,
} from './index.mjs';

export function diagnosticFields(fields) {
  return `<dl class="cr-facts">${fields.map(([label, value]) => `<div><dt>${E(label)}</dt><dd>${E(supplied(value))}</dd></div>`).join('')}</dl>`;
}

export function wakeEvidenceMarkup(item) {
  const title = wakeEvidenceLabels[item.state] || '证据不足';
  return `<details><summary>${E(title)} · ${E(dateTime(item.created_at))}</summary>${diagnosticFields(
    [
      ['原通知', identifier(item.notification_delivery_id)],
      ['接口受理', dateTime(item.accepted_at)],
      ['票据到期', dateTime(item.ticket_expires_at)],
      ['原回合', identifier(item.consumer_session_id)],
      ['实际领取', dateTime(item.claimed_at)],
      [
        '回合创建回复',
        item.reply_origin_truncated ? '至少 ' + item.created_reply_count : item.created_reply_count,
      ],
      ['保留的通知', identifier(item.retained_notification_delivery_id)],
    ],
  )}${item.claim_preceded_recorded_acceptance ? '<p>先观察到领取，随后保存接口受理回执。</p>' : ''}</details>`;
}

export async function loadWakeEvidence(button) {
  const current = panel;
  const target = button.closest('[data-cr-wake-evidence]');
  const cursor = button.dataset.cursor;
  if (!target || current?.name !== 'diagnostics') return;
  const params = new URLSearchParams({ member_id: button.dataset.id });
  if (cursor) params.set('cursor', cursor);
  button.disabled = true;
  try {
    const data = await api(store.path(current.roomId) + '/diagnostics?' + params);
    if (current !== panel || !target.isConnected) return;
    button.insertAdjacentHTML(
      'beforebegin',
      (data.items || []).map(wakeEvidenceMarkup).join('') ||
        '<p class="cr-muted">当前路线没有可显示的通知证据。</p>',
    );
    if (data.next_cursor) {
      button.dataset.cursor = data.next_cursor;
      button.textContent = '读取更早的通知';
    } else {
      button.remove();
      target.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
    }
  } finally {
    button.disabled = false;
  }
}

export function renderDiagnostics(data) {
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
    anomalies: '待核对项',
  };
  const traceLabels = {
    member: '成员',
    message: '原输入',
    goal: '工作',
    work: '步骤',
    operation: '原操作',
    delivery: '渠道投递',
    reply: '原回复',
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
    <section class="cr-inspection-section" data-cr-diagnostic-anomalies><h3>当前待核对</h3>${
      data.anomalies?.length
        ? `<ul>${data.anomalies.map((item) => `<li>${E(item.display?.title || wakeEvidenceLabels[item.state] || '证据待核对')} · ${E(dateTime(item.created_at))}</li>`).join('')}</ul>`
        : '<p class="cr-muted">本次查看范围内没有待核对项。</p>'
    }</section>
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
                )}<p class="cr-muted">宿主接口受理、票据领取和回复保存各有记录。${member.inbox?.complete === false ? '输入列表尚不完整。' : ''}</p>${member.notification?.latest_wake ? `<p>${E(wakeEvidenceLabels[member.notification.latest_wake.state] || '证据不足')}</p>` : ''}<div data-cr-wake-evidence>${btn('wake-evidence', '查看通知证据', `data-id="${E(member.member_id)}"`, 'cr-text-button')}</div></details>`,
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
                      ['原回合', 'consumer_session_id'],
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

export async function loadDiagnostics() {
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

export function contextCorrectionStale() {
  const correction = store.draft().contextCorrection;
  const context = state.room?.context;
  return !!(
    correction &&
    context &&
    (correction.version !== context.version ||
      correction.visibilityRevision !== context.visibility_revision)
  );
}

export function renderContext() {
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

export async function loadContext(more = false) {
  const current = panel,
    owner = state.owner;
  if (current?.name !== 'context' || current.loading) return;
  current.loading = true;
  const valid = () => panel === current && state.owner === owner && state.roomId === current.roomId;
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

export async function contextSource(id, sourceVersion) {
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

export function settingsPanel() {
  const room = state.room;
  openPanel(
    'settings',
    '房间设置',
    `<form data-cr-form="settings">${field('房间名称', `<input name="title" value="${E(room.title)}" required maxlength="120" autofocus>`)}<button type="submit" class="cr-button cr-primary">保存名称</button></form><section class="cr-setting-actions">${btn('context', '房间要点')}${btn('diagnostics', '运行诊断')}${btn('room-state', room.state === 'paused' ? '继续房间' : '暂停房间', `data-state="${room.state === 'paused' ? 'active' : 'paused'}"`)}${btn('archive-room', room.state === 'archived' ? '恢复房间' : '归档房间', '', 'cr-danger')}</section>`,
    { version: room.version },
  );
}
