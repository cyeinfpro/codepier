// Work: room UI behavior; lifecycle state is owned by index.mjs.
import {
  E,
  activeRoom,
  base,
  btn,
  deliveryMarkup,
  field,
  mutation,
  openPanel,
  panel,
  panelError,
  patchWorks,
  root,
  safeWorkActions,
  state,
  store,
  workLabels,
  workSourceId,
  workStatus,
} from './index.mjs';

export async function workPanel(id) {
  const work = state.works.find((item) => item.id === id);
  openPanel(
    'work',
    work?.title || '当前工作',
    '<div data-cr-work-view><p class="cr-muted">正在读取工作…</p></div>',
    { work: work || { id } },
  );
  state.workDetail = work || { id };
  if (work) renderWork(work);
  const current = panel;
  try {
    await store.work(id);
  } catch (error) {
    if (panel === current && [401, 403, 404].includes(error.status)) {
      root.querySelector('[data-cr-work-view]').textContent = '这项工作当前不可查看，请刷新房间。';
      state.works = state.works.filter((item) => item.id !== id);
      patchWorks();
    }
    throw error;
  }
  if (panel !== current) return;
  const latest =
    state.works.find((item) => item.id === id) ||
    (state.workDetail?.id === id ? state.workDetail : null);
  if (latest) renderWork(latest);
}

export function workRecoveryForm(action, candidates) {
  const takeover = action === 'takeover';
  const heading = takeover ? '接替这项工作的主责' : '恢复原成员的当前连接';
  return `<form class="cr-work-recovery" data-cr-form="work-recovery" data-work-action="${action}"><h3>${heading}</h3><p class="cr-muted">${takeover ? '只可选择这项工作原已批准且当前范围有效的成员。接替仅作用于此工作。' : '在同一工作中核对并恢复所选成员的连接。原操作及其结果继续保留，不自动重跑。'}</p>${field(takeover ? '接替成员' : '恢复成员', `<select name="target_member_id" required><option value="">请选择成员</option>${candidates.map((member) => `<option value="${E(member.member_id)}">${E(member.name || 'dot')} · #${E(member.member_id.slice(0, 8))}</option>`).join('')}</select>`)}<div class="cr-muted" data-cr-recovery-scope></div><button type="submit" class="cr-button cr-primary">${takeover ? '确认接替原工作' : '确认恢复原工作'}</button></form>`;
}

export function renderWork(work) {
  if (panel?.name !== 'work') return;
  const box = root?.querySelector('[data-cr-work-view]');
  if (!box) return;
  const actions = (work.allowed_actions || work.display?.allowed_actions || []).filter(
    (action) => activeRoom() || safeWorkActions.has(action),
  );
  const recovery = work.recovery || {};
  const coordinatorId = work.coordinator_member_id || recovery.coordinator_member_id;
  const team = work.members || [];
  const coordinator = team.find((member) => (member.member_id || member.id) === coordinatorId);
  const operations = work.operations || [];
  const workProject = (work.project_ids || []).find((id) =>
    S.projects.some((project) => project.id === id),
  );
  const reviewRevision = JSON.stringify([
    activeRoom(),
    work.version,
    work.intent_revision,
    work.coordinator_epoch || recovery.coordinator_epoch,
    recovery.restore_candidates || [],
    recovery.takeover_candidates || [],
  ]);
  const changedReview = panel.workReviewRevision && panel.workReviewRevision !== reviewRevision;
  const selected = [...box.querySelectorAll('form[data-cr-form="work-recovery"]')].map((form) => ({
    action: form.dataset.workAction,
    value: form.querySelector('select').value,
  }));
  const focusedAction = document.activeElement?.closest?.('[data-cr-form="work-recovery"]')?.dataset
    .workAction;
  panel.work = work;
  const markup = `<div class="cr-flow-state"><h3>${E(workStatus(work))}</h3><p>${E(work.display?.reason || work.reason_code || '')}</p></div>${!activeRoom() ? '<p class="cr-info">房间已暂停或归档。可以查看历史，也可以停止已有工作。</p>' : ''}${work.result_summary ? `<p>${E(work.result_summary)}</p>` : ''}${deliveryMarkup(work.channel_deliveries, work.delivery_display, work.channel_deliveries_truncated)}
    <section class="cr-work-summary">${work.acceptance ? `<h3>完成条件</h3><p>${E(work.acceptance)}</p>` : ''}${coordinatorId ? `<p data-cr-work-coordinator><strong>当前主责</strong> ${E(coordinator?.name || recovery.coordinator_name || 'dot')}</p>` : ''}${team.length ? `<h3>参与成员</h3><ul>${team.map((member) => `<li>${E(member.name || 'dot')}${(member.member_id || member.id) === coordinatorId ? ' · 主责' : ''}</li>`).join('')}</ul>` : ''}</section>
    <details class="cr-diagnostics cr-work-evidence"><summary>步骤与执行记录</summary>${work.items?.length ? `<ul>${work.items.map((item) => `<li><strong>${E(item.title || '工作步骤')}</strong> · ${E(workLabels[item.state] || item.state)}${item.display?.reason || item.reason_code ? `<p>${E(item.display?.reason || item.reason_code)}</p>` : ''}</li>`).join('')}</ul>` : '<p class="cr-muted">当前没有可显示的执行步骤。</p>'}<div data-cr-operation-receipts>${operations.length ? `<ul>${operations.map((operation) => `<li data-operation-id="${E(operation.id || operation.operation_id)}"><strong>${E(operation.display?.title || workLabels[operation.state] || operation.state)}</strong>${operation.cancel_requested && !['succeeded', 'failed', 'cancelled'].includes(operation.state) ? '<p>已请求停止，等待原操作回执</p>' : ''}${operation.display?.reason ? `<p>${E(operation.display.reason)}</p>` : ''}</li>`).join('')}</ul>` : '<p class="cr-muted">还没开始执行。</p>'}</div></details>
    ${recovery.state === 'waiting_operations' ? '<p class="cr-info">等当前操作结束后才能恢复或接替。</p>' : ''}
    ${actions.includes('restore_binding') && recovery.restore_candidates?.length ? workRecoveryForm('restore_binding', recovery.restore_candidates) : ''}
    ${actions.includes('takeover') && recovery.takeover_candidates?.length ? workRecoveryForm('takeover', recovery.takeover_candidates) : ''}
    ${workProject ? `<button type="button" class="cr-button cr-text-button" data-wf-action="project" data-project="${E(workProject)}">查看项目任务进度</button>` : ''}
    <div class="cr-work-controls">${actions
      .filter((action) => ['pause', 'resume', 'cancel', 'stop'].includes(action))
      .map((action) =>
        btn(
          'work-control',
          { pause: '暂停工作', resume: '继续原工作', cancel: '停止工作', stop: '停止工作' }[action],
          `data-work-action="${action}"`,
          ['cancel', 'stop'].includes(action) ? 'cr-danger' : 'cr-primary',
        ),
      )
      .join(
        '',
      )}${activeRoom() && workSourceId(work) ? btn('reply-work', '在对话中追问') : ''}${actions.includes('supplement') ? btn('supplement', '修改交办') : ''}</div>`;
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
    panelError('工作或连接范围已变化，请查看结果后重新选择成员。');
}

export function updateWorkRecoveryScope(form) {
  const action = form.dataset.workAction;
  const candidates =
    panel?.work.recovery?.[action === 'takeover' ? 'takeover_candidates' : 'restore_candidates'] ||
    [];
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

export async function controlWork(action, targetMemberId) {
  const work = panel.work;
  if (!(work.allowed_actions || work.display?.allowed_actions || []).includes(action)) {
    panelError('原工作当前不允许此动作，请重新读取后核对。');
    return;
  }
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
