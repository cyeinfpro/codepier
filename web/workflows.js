'use strict';
// Durable progress UI; commands are only run by the existing explicit tool paths.
function workflowState() {
  return (
    S.workflow || (S.workflow = { project: '', state: '', cursor: '', history: [], next: null })
  );
}
async function workflowsHTML(seq) {
  const w = { ...workflowState() },
    session = S.session;
  const r = await tool('workflows_list', {
    project: w.project,
    state: w.state,
    cursor: w.cursor,
    limit: 20,
  });
  if (seq !== S.renderSeq || session !== S.session) return '';
  workflowState().next = r.next_cursor;
  return (
    heading(
      '任务进度',
      '',
      'ChatGPT 记录的分步进度。',
      `<button class="btn ghost" data-action="refresh">${icon('refresh')}刷新</button><button class="btn ghost" data-wf-action="create">${icon('plus')}新建任务</button>`,
    ) +
    `<div class="filters"><select id="workflow-project" aria-label="筛选项目"><option value="">全部项目</option>${S.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === w.project ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select><select id="workflow-state" aria-label="筛选任务状态"><option value="">全部状态</option>${['active', 'blocked', 'completed', 'cancelled'].map((v) => `<option value="${v}" ${w.state === v ? 'selected' : ''}>${esc(stateNames[v])}</option>`).join('')}</select></div>` +
    `<section class="workflow-cards">${r.workflows.length ? r.workflows.map((w) => `<article class="panel workflow-card" data-workflow-state="${esc(w.state)}"><div class="card-top"><span class="eyebrow">${esc(w.project_alias)}</span>${badge(w.state)}</div><h2>${esc(w.title)}</h2><p class="workflow-goal">${esc(w.goal)}</p><div class="workflow-progress"><strong>${w.progress.completed}<small> / ${w.progress.total} 步完成</small></strong><span class="muted tiny">${w.progress.skipped ? `${w.progress.skipped} 步已说明跳过` : ''}</span></div><progress max="${w.progress.total}" value="${w.progress.completed}" aria-label="已完成步骤"></progress><div class="workflow-card-foot"><span class="muted tiny">${esc(timeText(w.updated))}</span><button class="btn small" data-wf-action="detail" data-id="${esc(w.workflow_id)}">查看任务 ${icon('arrow')}</button></div></article>`).join('') : empty('还没有任务。')}</section>` +
    `<div class="pagination"><div class="actions"><button class="btn ghost small" data-wf-action="prev" ${w.history.length ? '' : 'disabled'}>上一页</button><button class="btn ghost small" data-wf-action="next" ${r.next_cursor ? '' : 'disabled'}>下一页</button></div></div>`
  );
}
function bindWorkflows() {
  for (const [id, key] of [
    ['workflow-project', 'project'],
    ['workflow-state', 'state'],
  ]) {
    $('#' + id).onchange = (e) => {
      Object.assign(workflowState(), { [key]: e.target.value, cursor: '', history: [] });
      renderPage(false).catch((err) => toast(err.message, true));
    };
  }
}
function bindWorkflowSubmit(dialog, form, button, name, readArgs, done) {
  let pending = null;
  const session = S.session;
  const controls = () => $$('input,textarea,select', form);
  const submit = () =>
    busy(button, async () => {
      if (!pending) {
        if (!form.reportValidity()) return;
        pending = { ...readArgs(), idempotency_key: 'panel-workflow-' + uid() };
        controls().forEach((el) => (el.disabled = true));
      }
      try {
        const result = await tool(name, pending);
        if (session === S.session && dialog.isConnected) await done(result);
      } catch (error) {
        if (dialog.isConnected && session === S.session) {
          const known =
            error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status);
          if (known) {
            pending = null;
            controls().forEach((el) => (el.disabled = false));
          }
          const hint = $('[data-workflow-hint]', form);
          if (hint)
            hint.textContent = known
              ? error.code === 'VERSION_CONFLICT'
                ? '进度已被其他客户端更新。请重新读取任务，再合并本次进度。'
                : error.message
              : '网络中断，保存结果待确认。可以重试当前请求。';
        }
        throw error;
      }
    });
  button.onclick = submit;
  form.onsubmit = (e) => {
    e.preventDefault();
    submit();
  };
}
async function workflowCreate() {
  const session = S.session,
    loading = modal('新建任务', '<p class="muted">读取可用项目…</p>');
  await loadBasics();
  if (session !== S.session || !loading.isConnected) return;
  const projects = S.projects.filter((p) => p.mode === 'write');
  if (!projects.length) {
    closeModal(loading);
    toast('请先添加一个可写项目', true);
    return;
  }
  const selected = workflowState().project || S.work.project;
  const dialog = modal(
    '新建任务',
    `<form id="workflow-create-form">
      <div class="field"><label for="wf-project">项目</label><select id="wf-project" name="project">${projects.map((p) => `<option value="${esc(p.id)}" ${p.id === selected ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select></div>
      <div class="field"><label for="wf-goal">要完成什么</label><textarea id="wf-goal" name="goal" required maxlength="2000" rows="3" placeholder="例如：修复上传。请勿填写密码或令牌。"></textarea></div>
      <details class="form-advanced"><summary>更多选项</summary>
        <div class="field"><label for="wf-title">任务名称</label><input id="wf-title" name="title" maxlength="140" placeholder="默认取目标的第一行"></div>
        <div class="field"><label for="wf-assignee">交接对象</label><select id="wf-assignee" name="assignee_grant_id"><option value="">仅面板管理</option></select><small id="wf-assignee-status" role="status"></small></div>
        <div class="field"><label for="wf-template">检查清单</label><select id="wf-template" name="template"><option value="review_fix">检查与修复</option><option value="release">验证与发布</option></select></div>
      </details><p class="form-note" data-workflow-hint role="status"></p></form>`,
    buttons('workflow-create-save', '建立任务'),
  );
  let assigneeSequence = 0;
  const select = $('#wf-assignee', dialog),
    projectSelect = $('#wf-project', dialog),
    status = $('#wf-assignee-status', dialog);
  const current = () => session === S.session && dialog.isConnected;
  const refreshAssignees = async () => {
    const ticket = ++assigneeSequence,
      project = projectSelect.value;
    select.replaceChildren(new Option('仅面板管理', ''));
    status.textContent = '';
    try {
      const result = await api(
        '/api/projects/' + encodeURIComponent(project) + '/workflow-assignees',
      );
      if (!current() || ticket !== assigneeSequence || result.project_id !== project) return;
      for (const grant of result.assignees) select.add(new Option(grant.label, grant.id));
    } catch (error) {
      if (current() && ticket === assigneeSequence) status.textContent = '交接对象暂不可用。';
    }
  };
  projectSelect.onchange = refreshAssignees;
  bindWorkflowSubmit(
    dialog,
    $('#workflow-create-form'),
    $('#workflow-create-save'),
    'workflows_create',
    () => {
      const args = Object.fromEntries(new FormData($('#workflow-create-form', dialog)));
      args.title = args.title.trim() || args.goal.trim().split(/\r?\n/)[0].slice(0, 40);
      if (!args.assignee_grant_id) delete args.assignee_grant_id;
      return args;
    },
    async (r) => {
      closeModal(dialog);
      if (S.page === 'workflows') await renderPage(false);
      await workflowDetail(r.workflow_id);
    },
  );
  void refreshAssignees();
}
function workflowEvidenceHTML(items) {
  return (items || [])
    .map(
      (e) =>
        `<button class="btn ghost small workflow-evidence" data-wf-action="evidence" data-id="${esc(e.operation_id)}">${icon('terminal')} ${esc(e.tool)} · ${esc(e.updated ? shortTime(e.updated) : '查看结果')} ${e.exit_code === null ? '' : `/ 退出码 ${esc(e.exit_code)}`}</button>`,
    )
    .join('');
}
async function workflowDetail(id, before = null, fresh = null) {
  if (!S.session) return;
  const session = S.session,
    loading = fresh ? null : modal('读取任务', '<p class="muted">正在读取已保存的进度…</p>');
  try {
    const w =
      fresh ||
      (await tool('workflows_get', {
        workflow_id: id,
        ...(before ? { before_event_id: before } : {}),
      }));
    if (session !== S.session || (loading && !loading.isConnected)) return;
    const mutable =
      w.can_update !== false && !w.mapping_changed && !['completed', 'cancelled'].includes(w.state);
    const dialog = modal(
      w.title,
      `<div class="workflow-detail-meta"><span>${esc(w.project_alias)} · ${w.assigned_to_mcp ? '已交接 MCP 授权' : '仅面板管理'}</span>${badge(w.state)}</div><p class="workflow-summary">${esc(w.goal)}</p>${w.mapping_changed ? notice('项目位置已改，这个任务只能查看。') : ''}<div class="workflow-steps">${w.steps.map((s, i) => `<section class="workflow-step"><div class="workflow-step-head"><span class="step-number">${i + 1}</span><h3>${esc(s.title)}</h3>${badge(s.state)}</div><p class="muted">${esc(s.acceptance)}</p>${s.summary ? `<p class="workflow-summary">${esc(s.summary)}</p>` : ''}<div class="workflow-evidence-list">${workflowEvidenceHTML(s.evidence)}</div></section>`).join('')}</div><h3>当前摘要</h3><p class="workflow-summary">${esc(w.summary || '尚未记录。')}</p><details class="workflow-history"><summary>${before ? '较早的' : '最近的'}进度记录</summary>${w.events.map((e) => `<article><div class="muted tiny">${esc(timeText(e.at))} · v${e.version} · ${esc(e.action)}</div><p class="workflow-summary">${esc(e.summary)}</p>${workflowEvidenceHTML(e.evidence)}</article>`).join('')}${w.next_before_event_id ? `<button class="btn ghost small" data-wf-action="older" data-id="${esc(id)}" data-before="${w.next_before_event_id}">更早记录</button>` : ''}${before ? `<button class="btn ghost small" data-wf-action="detail" data-id="${esc(id)}">回到最新记录</button>` : ''}</details><p class="form-note"></p>`,
      `${uiMenu('更多', `<button class="btn ghost" data-devtools="handoff" data-project="${esc(w.project_id)}" data-workflow="${esc(id)}">交给开发工具</button><button class="btn ghost" data-devtools="validation" data-project="${esc(w.project_id)}">测试</button><button class="btn ghost" data-action="project-collaboration" data-id="${esc(w.project_id)}">协作中心</button>${typeof chatProjectButtons === 'function' ? chatProjectButtons(w.project_id) : ''}`)}${mutable ? '<button class="btn primary" id="workflow-edit">记录进度</button>' : '<button class="btn primary" data-action="close-modal">关闭</button>'}`,
      true,
    );
    if (mutable) $('#workflow-edit', dialog).onclick = () => workflowEdit(w);
    if (!before && mutable) {
      const space = S.space_id,
        page = S.page;
      const current = () =>
        dialog.isConnected && session === S.session && space === S.space_id && page === S.page;
      let timer;
      const schedule = () => {
        if (current()) timer = setTimeout(refresh, 5000);
      };
      const refresh = async () => {
        if (!current()) return;
        try {
          const updated = await tool('workflows_get', { workflow_id: id });
          if (!current()) return;
          if (updated.version !== w.version || updated.mapping_changed !== w.mapping_changed) {
            await workflowDetail(id, null, updated);
            return;
          }
        } catch (error) {
          if (current() && [401, 403, 404].includes(error.status)) {
            closeModal(dialog);
            toast(error.message, true);
            return;
          }
        }
        schedule();
      };
      const cleanup = S.modalCleanup;
      S.modalCleanup = () => {
        clearTimeout(timer);
        cleanup?.();
      };
      schedule();
    }
  } catch (error) {
    if (session === S.session && loading?.isConnected) {
      $('.modal-body', loading).innerHTML = notice(esc(error.message));
    } else if (session === S.session) {
      return;
    }
    throw error;
  }
}
function workflowEdit(w) {
  const actions =
    w.state === 'blocked'
      ? [
          ['resume', '解除阻碍'],
          ['block', '补充阻碍'],
          ['cancel', '取消任务'],
        ]
      : [
          ['checkpoint', '记录步骤'],
          ['block', '标记受阻'],
          ['complete', '标记完成'],
          ['cancel', '取消任务'],
        ];
  const dialog = modal(
    '记录进度 · ' + w.title,
    `<form id="workflow-update-form">
      <fieldset class="workflow-action-options"><legend>记录什么</legend>${actions.map(([value, label]) => `<label class="check"><input type="radio" name="action" value="${value}" ${value === actions[0][0] ? 'checked' : ''}>${label}</label>`).join('')}</fieldset>
      <div id="workflow-step-fields" ${w.state === 'blocked' ? 'hidden' : ''}><div class="form-row">
        <div class="field"><label for="wf-step">步骤</label><select name="step_id" id="wf-step"><option value="">仅记录断点</option>${w.steps.map((step) => `<option value="${esc(step.id)}">${esc(step.title)}</option>`).join('')}</select></div>
        <div class="field"><label for="wf-step-state">步骤状态</label><select name="step_state" id="wf-step-state"><option value="completed">已完成</option><option value="running">进行中</option><option value="pending">待处理</option><option value="skipped">已跳过</option></select></div>
      </div></div>
      <div class="field"><label for="wf-summary" id="wf-summary-label">实际结果</label><textarea id="wf-summary" name="summary" required maxlength="4000" rows="4"></textarea></div>
      <div class="field" id="wf-evidence"><p>选这一步用到的操作。</p><div class="check-list" id="wf-evidence-list" role="status">正在读取操作…</div></div>
      <p class="form-note" data-workflow-hint role="status">跳过步骤请写明原因。</p></form>`,
    buttons('workflow-update-save', '保存进度'),
  );
  const form = $('#workflow-update-form', dialog);
  const evidence = $('#wf-evidence', dialog);
  const sync = () => {
    const action = form.elements.action.value;
    $('#workflow-step-fields', dialog).hidden = action !== 'checkpoint';
    evidence.hidden = !['checkpoint', 'complete'].includes(action);
    $('#wf-summary-label', dialog).textContent =
      { block: '遇到的阻碍', resume: '解决方式', complete: '检查结果', cancel: '取消原因' }[
        action
      ] || '实际结果';
  };
  form.addEventListener('change', sync);
  sync();
  const list = $('#wf-evidence-list', dialog);
  api(
    '/api/operations?' +
      new URLSearchParams({ project: w.project_id, status: 'succeeded', limit: '20' }),
  ).then(
    ({ operations }) => {
      if (!dialog.isConnected) return;
      const rows = operations.filter((op) => op.created >= (w.created || 0));
      list.innerHTML =
        rows
          .map(
            (op) =>
              `<label class="check"><input type="checkbox" name="evidence" value="${esc(op.id)}"><span>${esc(op.tool)} · ${esc(shortTime(op.created))}</span></label>`,
          )
          .join('') || '<small>此任务建立后还没有成功的操作。</small>';
    },
    (error) => {
      if (dialog.isConnected) list.textContent = error.message;
    },
  );
  bindWorkflowSubmit(
    dialog,
    $('#workflow-update-form'),
    $('#workflow-update-save'),
    'workflows_update',
    () => {
      const f = Object.fromEntries(new FormData(form)),
        args = {
          workflow_id: w.workflow_id,
          expected_version: w.version,
          action: f.action,
          summary: f.summary,
          evidence: evidence.hidden
            ? []
            : $$('[name="evidence"]:checked', form).map((input) => input.value),
        };
      if (f.action === 'checkpoint' && f.step_id) {
        args.step_id = f.step_id;
        args.step_state = f.step_state;
      }
      return args;
    },
    async (r) => {
      closeModal(dialog);
      if (S.page === 'workflows') await renderPage(false);
      await workflowDetail(r.workflow_id);
    },
  );
}
async function workflowContext() {
  const { project, workspace_id = '' } = workTarget(),
    session = S.session;
  if (!project) {
    toast('请先选择项目', true);
    return;
  }
  const loading = modal('项目上下文', '<p class="muted">读取入口文档与技能索引…</p>');
  try {
    const r = await settled(tool('project_context', { project, workspace_id }));
    if (session !== S.session || !loading.isConnected || project !== S.work.project) return;
    modal(
      '项目上下文 · ' + (r.project || ''),
      notice('这是入口文档预览，不是全仓扫描。技能只建立索引，不会自动加载或执行。', true) +
        `<div class="spacer"></div><p class="muted tiny">${r.budget.preview_chars} / ${r.budget.max_chars} 字符 · ${r.documents.length} 份预览 · ${r.skills.length} 项技能${r.truncated ? ' · 含截断或未读取项' : ''}</p>${r.documents.map((d) => `<details class="context-document"><summary>${esc(d.path)} ${d.truncated ? '· 已截断' : ''}</summary><pre>${esc(d.content)}</pre><button class="btn ghost small" data-wf-action="document" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-path="${esc(d.path)}">按页读取原文</button></details>`).join('')}<h3>技能索引</h3>${r.skills.map((s) => `<div class="context-skill"><div><strong>${esc(s.name)}</strong><p class="muted">${esc(s.description)}</p></div><button class="btn small" data-wf-action="document" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-path="${esc(s.path)}">读取</button></div>`).join('') || '<p class="muted">没有在指定目录发现技能文件。</p>'}<details class="context-document"><summary>执行能力、未读取项与扫描提示</summary><pre>${esc(json({ execution: r.execution, remaining_documents: r.remaining_documents, warnings: r.warnings, scope: r.scope }))}</pre></details>`,
      '',
      true,
    );
  } catch (error) {
    if (loading.isConnected && session === S.session)
      $('.modal-body', loading).innerHTML = notice(esc(error.message));
    throw error;
  }
}
async function workflowReadDocument(project, path, start = 1, workspace_id = '') {
  const session = S.session,
    loading = modal(path, '<p class="muted">读取文档…</p>');
  const r = await settled(
    tool('fs_read', { project, workspace_id, path, start_line: start, max_lines: 400 }),
  );
  if (session !== S.session || !loading.isConnected) return;
  modal(
    path,
    `<p class="muted tiny">第 ${r.start_line}–${r.end_line} 行 / 共 ${r.total_lines} 行 · SHA ${esc(r.sha256.slice(0, 16))}</p><div class="code-box"><pre>${esc(r.content)}</pre></div>`,
    r.next_start_line
      ? `<button class="btn primary" data-wf-action="document" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-path="${esc(path)}" data-start="${r.next_start_line}">继续读取</button>`
      : '',
    true,
  );
}
async function workflowEvidence(id) {
  const session = S.session,
    loading = modal('操作详情', '<p class="muted">读取实际执行记录…</p>');
  const r = await tool('operations_get', { operation_id: id });
  if (session !== S.session || !loading.isConnected) return;
  modal(
    '操作详情 · ' + r.tool,
    `<p><code>${esc(id)}</code> ${badge(r.state)}</p><div class="code-box"><pre>${esc(json(r))}</pre></div>`,
    '',
    true,
  );
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-wf-action]');
  if (!b || b.disabled || !S.session) return;
  try {
    const w = workflowState();
    switch (b.dataset.wfAction) {
      case 'create':
        await workflowCreate();
        break;
      case 'project':
        if (!S.projects.some((project) => project.id === b.dataset.project)) break;
        Object.assign(w, { project: b.dataset.project, state: '', cursor: '', history: [] });
        closeModal();
        if (S.page === 'workflows') await renderPage(false);
        else await navigate('workflows');
        break;
      case 'detail':
        await workflowDetail(b.dataset.id);
        break;
      case 'older':
        await workflowDetail(b.dataset.id, Number(b.dataset.before));
        break;
      case 'context':
        await workflowContext();
        break;
      case 'document':
        await workflowReadDocument(
          b.dataset.project,
          b.dataset.path,
          Number(b.dataset.start || 1),
          b.dataset.workspace || '',
        );
        break;
      case 'evidence':
        await workflowEvidence(b.dataset.id);
        break;
      case 'next':
        if (w.next) {
          w.history.push(w.cursor);
          w.cursor = w.next;
          await renderPage(false);
        }
        break;
      case 'prev':
        if (w.history.length) {
          w.cursor = w.history.pop();
          await renderPage(false);
        }
        break;
    }
  } catch (error) {
    toast(error.message, true);
  }
});
