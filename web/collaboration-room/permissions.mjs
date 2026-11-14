// Permissions: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  announce,
  btn,
  field,
  mutation,
  openPanel,
  panel,
  panelError,
  patchRoomPermissions,
  permissionCaps,
  permissionKey,
  permissionLevel,
  permissionLevels,
  permissionOptions,
  readOnlyReason,
  reasonText,
  retryableContinuationPreview,
  returnToChannel,
  root,
  selection,
  state,
  store,
} from './index.mjs';

export function permissionDateValue(timestamp) {
  if (!Number.isFinite(Number(timestamp)) || timestamp == null) return '';
  const value = new Date(Number(timestamp) * 1000);
  return new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}

export function permissionLifetimeControls(lifetime, inherit) {
  const value = lifetime || { mode: 'until_revoked', expires_at: null };
  return `<div data-permission-lifetime data-original-lifetime="${E(JSON.stringify(value))}" data-inherit-lifetime="${inherit}">${field('有效期', `<select data-permission-lifetime-mode><option value="until_revoked" ${value.mode === 'until_revoked' ? 'selected' : ''}>直到我撤销</option><option value="timed" ${value.mode === 'timed' ? 'selected' : ''}>指定截止时间</option></select>`)}${field('截止时间（本地时间）', `<input type="datetime-local" step="1" data-permission-lifetime-until value="${E(permissionDateValue(value.expires_at))}">`)}<small class="cr-warning" data-permission-expired hidden>已过期：请在这里选择新的截止时间或改为直到我撤销，再保存续期。</small>${inherit ? '<small class="cr-muted">新自定义范围默认继承当前房间项目有效期；不能晚于房间截止时间。</small>' : ''}</div>`;
}

export function permissionLifetimeValue(group) {
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

export function permissionRows(policy, scope, section) {
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
            <span class="cr-permission-project">${E(label)}${env === 'production' ? '' : ' · ' + E(env)}</span>
            <input type="checkbox" data-permission-project hidden tabindex="-1" aria-hidden="true" ${selected.length ? 'checked' : ''}>
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

export function permissionSection(policy, scope, section) {
  // One dropdown per project decides whether it joins and at which level. The
  // bulk control sets every project the user can grant; nothing is saved until
  // 「保存权限」.
  return `<section data-permission-section="${section}"><div class="cr-permission-bulk">
    <span class="cr-muted" data-permission-count aria-live="polite"></span>
    <label>全部项目设为 <select data-permission-bulk-level><option value="-" selected>选择档位</option>${permissionOptions('-', true)}</select></label>
    ${section === 'room' ? '<label>已加入项目有效期 <select data-permission-global-expiry><option value="">各项目设置</option><option value="until_revoked">直到撤销</option><option value="timed">指定时间</option></select></label><label data-permission-global-date hidden>截止时间 <input type="datetime-local"></label>' : ''}
    </div>${permissionRows(policy, scope, section)}</section>`;
}

export function renderPermissions(policy) {
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

export async function permissionsPanel(returnChannel = null) {
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

export function updatePermissionControls() {
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
    const parentEnabled = parent?.querySelector('[data-permission-project]:checked:not(:disabled)');
    project.disabled =
      !group.querySelector('[data-host-available="true"]') ||
      (channel !== 'room' && !parentEnabled);
    const enabled = project.checked && !project.disabled;
    const lifetimeMode = group.querySelector('[data-permission-lifetime-mode]');
    const lifetimeUntil = group.querySelector('[data-permission-lifetime-until]');
    const expiryBox = group.querySelector('.cr-permission-expiry');
    if (expiryBox) expiryBox.hidden = !enabled;
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
    expiry.querySelector('summary').textContent = !group.querySelector('[data-permission-expired]')
      .hidden
      ? '有效期 · 已过期'
      : '有效期';
  }
  for (const section of form.querySelectorAll('[data-permission-section]')) {
    const projects = [...section.querySelectorAll('[data-permission-project]:not(:disabled)')];
    const selected = projects.filter((project) => project.checked).length;
    section.querySelector('[data-permission-count]').textContent =
      '已加入 ' + selected + ' / ' + projects.length;
    section.querySelector('[data-permission-bulk-level]').disabled = !projects.length;
    for (const control of section.querySelectorAll(
      '[data-permission-global-expiry], [data-permission-global-date] input',
    ))
      control.disabled = !selected;
  }
  patchRoomPermissions();
}

export function applyPermissionLevel(select, value, allTargets = false) {
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

export function applyGlobalPermissionExpiry(section) {
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

export function collectPermissionScope(section, parentScope = null) {
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

export function restrictHiddenChannel(prior, parent) {
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

export function continuationReason(code) {
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

export function continuationSummary(result) {
  const counts = result.counts || {};
  return `续办：${counts.continued || 0} 项已继续，${counts.waiting_operations || 0} 项待核对，${counts.skipped || 0} 项跳过${counts.pending ? '，' + counts.pending + ' 项待处理' : ''}。`;
}

export function renderContinuationResult(current) {
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

export async function refreshContinuationResult(current) {
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

export function updateContinuationSelection(form) {
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

export function continuationPreviewMarkup(preview) {
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

export async function previewContinuation(form, current) {
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
    box.innerHTML = `<p role="status">${E(error.message || '暂时无法核对续办清单。')}</p>${btn('refresh-continuation-preview', '重试', 'title="沿用原请求，不会重复执行"', 'cr-text-button')}`;
  }
  patchRoomPermissions();
}

export function permissionDraft(form, current) {
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

export function pausedProjectNotice(result) {
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

export async function savePermissions(form, current) {
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
