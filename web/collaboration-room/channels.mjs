// Channels: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  actionLabel,
  activeRoom,
  announce,
  base,
  btn,
  channelCopy,
  channelSteps,
  dateTime,
  facts,
  field,
  fileSize,
  icon,
  identifier,
  memberId,
  migrationBlockerMarkup,
  mutation,
  openPanel,
  panel,
  panelEpoch,
  panelError,
  patchRoomPermissions,
  quietInbound,
  readOnlyReason,
  reasonText,
  roomName,
  root,
  safeChannelActions,
  safeImage,
  stages,
  state,
  store,
  title,
} from './index.mjs';

export async function weixinPanel() {
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

export async function startWeixin(current) {
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

export function inboundHealthMarkup(accounts) {
  const items = (accounts || []).flatMap((account) =>
    (account.inbound_health || [])
      .filter((item) => ['deferred', 'quarantined'].includes(item.state) && Number(item.count) > 0)
      .map(
        (item) =>
          `<li><strong>${E(account.label || account.bot_id || '当前账号')}</strong> · ${item.state === 'deferred' ? '等待积压消化' : '已隔离'} ${E(item.count)} 条 · <code>${E(item.reason_code || 'unknown')}</code></li>`,
      ),
  );
  return items.length
    ? `<section class="cr-info" aria-label="渠道收件状态"><strong>部分消息需要留意</strong><ul>${items.join('')}</ul><p>延后消息保留在原连接，积压消化后自动核对；隔离消息不会执行任务。后续正常消息仍可接收。</p></section>`
    : '';
}

export function uploadEstimateMarkup(estimate) {
  if (!estimate || !Number.isFinite(Number(estimate.estimated_seconds))) return '';
  const seconds = Math.max(0, Math.ceil(Number(estimate.estimated_seconds)));
  const duration = seconds >= 60 ? Math.ceil(seconds / 60) + ' 分钟' : seconds + ' 秒';
  return `<p class="cr-muted" data-cr-upload-estimate>附件外发参考：${estimate.reference_bytes ? E(fileSize(estimate.reference_bytes)) + ' 文件' : '所选文件'}约 ${E(duration)}。 ${E(estimate.note || '')} 外发文件仍需单独批准。</p>`;
}

export function channelLiveParts(box, flow) {
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

export function channelIdentity(nickname, id) {
  if (nickname) return nickname;
  const value = String(id || '');
  return value.length > 8
    ? value.slice(0, 4) + '…' + value.slice(-4)
    : value
      ? value.slice(0, 1) + '…'
      : '';
}

export function channelScopeMarkup(preview, flow) {
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
      ['连接保持', renewal ? '由 dot 宿主续订' : null],
      ['当前订阅到期', renewal?.expires_at ? dateTime(renewal.expires_at) : null],
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

export function renderChannel(result) {
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
  const followPreview = ['bind', 'approve_tasks'].includes(action) ? flow.follow_task_scope : null;
  const allowedActions = flow.allowed_actions || (flow.actions || []).map((item) => item.action);
  const writable = activeRoom();
  const stage = flow.stage;
  const accounts =
    writable && stage === 'choose_account' && allowedActions.includes('select_account')
      ? flow.accounts || []
      : [];
  const label = flow.actions?.find((item) => item.action === action)?.label || actionLabel(action);
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
      (action === 'approve_tasks' && !flow.task_scope && followPreview?.project_ids?.length === 0);
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

export function updateChannelDependencies() {
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

export function channelField(item) {
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

export async function submitChannel(form) {
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

export async function refreshChannel() {
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

export async function recoverSend(id) {
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
  } else if (result.not_saved === true && store.writable(receipt.roomId)) await store.send(receipt);
  else if (result.not_saved === true)
    announce('原消息尚未保存。房间当前只读，原草稿与请求已保留，没有重新发送。');
  else announce('原消息仍在核对，恢复连接后继续查看。');
}
