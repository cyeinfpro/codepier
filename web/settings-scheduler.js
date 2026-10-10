'use strict';
window.CodePierSchedulerSettings = (() => {
  let state = { owner: '', device: '', data: null, draft: null, pending: false };
  let root = null,
    controller = null,
    generation = 0,
    timer = null;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const stable = (value) =>
    Array.isArray(value)
      ? value.map(stable)
      : value && typeof value === 'object'
        ? Object.fromEntries(
            Object.keys(value)
              .sort()
              .map((key) => [key, stable(value[key])]),
          )
        : value;
  const equal = (a, b) => JSON.stringify(stable(a)) === JSON.stringify(stable(b));
  const owner = () => (S.session?.user_id || '') + ':' + S.space_id;
  const dirty = () => !!state.data && !equal(state.draft, state.data.config);
  const labels = {
    starting: '正在采样',
    fixed: '固定上限',
    healthy: '资源充足',
    cooldown: '观察负载',
    resource_pressure: '负载较高，减少新任务',
    metrics_unavailable: '采样不可用，保守调度',
    memory_critical: '内存紧张，暂停新任务',
    memory_recovery: '等待内存稳定',
    recovering: '逐步恢复',
  };
  const statusLabels = {
    offline: '节点离线，设置已保留',
    unsupported: '升级 Agent 后生效',
    pending: '等待节点确认',
    applied: '已生效',
    rejected: '节点未接受，保留原设置',
  };

  function detach() {
    controller?.abort();
    controller = null;
    clearTimeout(timer);
    timer = null;
    root = null;
    generation++;
  }
  function reset() {
    detach();
    state = { owner: '', device: '', data: null, draft: null, pending: false };
  }
  function discard() {
    if (state.data) state.draft = clone(state.data.config);
  }
  function number(name, title, value) {
    return (
      '<label class="field">' +
      esc(title) +
      '<input type="number" min="1" max="64" step="1" name="' +
      name +
      '" placeholder="继承节点" value="' +
      esc(value ?? '') +
      '"></label>'
    );
  }
  function metrics(data) {
    const info = data.reported;
    if (!info) return '<p class="form-note">等待节点报告运行情况</p>';
    return (
      '<div class="settings-tags"><span>运行 ' +
      info.running +
      '</span><span>排队 ' +
      info.queued +
      '</span><span>' +
      esc(labels[info.reason] || '等待采样') +
      '</span></div><p class="form-note">当前容量：执行 ' +
      info.lanes.execution.capacity +
      ' · 文件 ' +
      info.lanes.read.capacity +
      ' · 远程 ' +
      info.lanes.remote.capacity +
      '</p>'
    );
  }
  async function html(selectedProject = '') {
    if (state.owner !== owner()) {
      reset();
      state.owner = owner();
    }
    const expectedOwner = owner(),
      session = S.session,
      token = ++generation;
    const [deviceResult, projectResult] = await Promise.all([
      api('/api/devices'),
      api('/api/projects'),
    ]);
    const devices = deviceResult.devices.filter((device) => device.can_manage);
    const visibleProjects = projectResult.projects;
    if (token !== generation || expectedOwner !== owner() || session !== S.session)
      throw sessionChanged();
    if (!devices.length) return '';
    const project = visibleProjects.find((item) => item.id === selectedProject);
    if (!devices.some((device) => device.id === state.device)) {
      state.device = devices.some((device) => device.id === project?.device_id)
        ? project.device_id
        : devices[0].id;
      state.data = null;
      state.draft = null;
    }
    const fresh = await api('/api/settings/scheduler?device=' + encodeURIComponent(state.device));
    if (token !== generation || expectedOwner !== owner() || session !== S.session)
      throw sessionChanged();
    if (!dirty()) {
      state.data = fresh;
      state.draft = clone(fresh.config);
    }
    const draft = state.draft,
      data = state.data;
    const projects = visibleProjects.filter((item) => item.device_id === state.device);
    return (
      '<section class="panel settings-card" id="settings-scheduler"><div class="panel-head"><h2>并行与队列</h2>' +
      '<span class="settings-mode" data-scheduler-state>' +
      esc(statusLabels[data.state] || '待核对') +
      '</span></div><div class="panel-body">' +
      '<label class="field">节点<select id="scheduler-device">' +
      devices
        .map(
          (device) =>
            '<option value="' +
            esc(device.id) +
            '"' +
            (device.id === state.device ? ' selected' : '') +
            '>' +
            esc(device.name) +
            '</option>',
        )
        .join('') +
      '</select></label>' +
      '<div data-scheduler-metrics>' +
      metrics(data) +
      '</div><form id="scheduler-settings-form"><div class="settings-import-fields">' +
      '<label class="field">调节方式<select name="adaptive"><option value="">继承节点</option><option value="true"' +
      (draft.adaptive === true ? ' selected' : '') +
      '>自动调节</option><option value="false"' +
      (draft.adaptive === false ? ' selected' : '') +
      '>固定上限</option></select></label>' +
      number('maximum', '执行并发上限', draft.maximum) +
      '</div>' +
      '<details class="settings-details"><summary>项目配额与高级设置</summary><p>新设置只影响后续准入。节点本机上限和资源保护仍生效。</p>' +
      '<div class="settings-import-fields">' +
      number('minimum', '执行最低容量', draft.minimum) +
      number('initial', '初始 / 固定容量', draft.initial) +
      number('read_limit', '文件操作上限', draft.read_limit) +
      number('remote_limit', '远程操作上限', draft.remote_limit) +
      number('remote_target_limit', '单个远程目标上限', draft.remote_target_limit) +
      '</div>' +
      '<div class="settings-import-fields">' +
      projects
        .map(
          (project) =>
            '<label class="field">' +
            esc(project.alias) +
            '<input type="number" min="1" max="64" step="1" data-scheduler-project="' +
            esc(project.id) +
            '" placeholder="共享空闲余量" value="' +
            esc(draft.project_limits?.[project.id] ?? '') +
            '"></label>',
        )
        .join('') +
      (data.stale_project_limits || [])
        .filter((id) => Object.hasOwn(draft.project_limits || {}, id))
        .map(
          (id, index) =>
            '<label class="field" data-scheduler-stale-project>失效项目配额 ' +
            (index + 1) +
            '<input type="number" readonly data-scheduler-project="' +
            esc(id) +
            '" value="' +
            esc(draft.project_limits[id]) +
            '"><span class="form-note">项目已删除或移离当前节点；移除后保存。</span>' +
            '<button class="btn ghost" type="button" data-scheduler-remove-project="' +
            esc(id) +
            '">移除此配额</button></label>',
        )
        .join('') +
      '</div></details>' +
      '<div class="actions"><button class="btn primary" type="submit">保存</button><button class="btn ghost" type="button" data-scheduler-reset>取消改动</button>' +
      '<button class="btn ghost" type="button" data-scheduler-refresh>刷新状态</button></div>' +
      '<p class="form-note" role="status" data-scheduler-feedback></p></form></div></section>'
    );
  }
  function bind() {
    detach();
    root = $('#settings-scheduler');
    if (!root) return;
    controller = new AbortController();
    const mounted = root,
      captured = state,
      session = S.session,
      token = generation,
      expectedOwner = owner(),
      signal = controller.signal;
    const current = () =>
      !signal.aborted &&
      mounted.isConnected &&
      root === mounted &&
      token === generation &&
      expectedOwner === owner() &&
      session === S.session &&
      S.page === 'settings';
    const listen = (node, event, callback) => node?.addEventListener(event, callback, { signal });
    const form = $('#scheduler-settings-form', mounted);
    const feedback = (text) => {
      if (current()) $('[data-scheduler-feedback]', mounted).textContent = text;
    };
    const repaint = (data) => {
      if (!current()) return;
      $('[data-scheduler-state]', mounted).textContent = statusLabels[data.state] || '待核对';
      $('[data-scheduler-metrics]', mounted).innerHTML = metrics(data);
      window.CodePierSettings?.refreshDraftNote();
    };
    const readDraft = () => {
      const value = clone(state.draft || {});
      const adaptive = form.elements.adaptive.value;
      if (adaptive === '') delete value.adaptive;
      else value.adaptive = adaptive === 'true';
      for (const name of [
        'minimum',
        'maximum',
        'initial',
        'read_limit',
        'remote_limit',
        'remote_target_limit',
      ]) {
        const raw = form.elements[name].value;
        if (raw === '') delete value[name];
        else value[name] = Number(raw);
      }
      const projects = { ...(value.project_limits || {}) };
      for (const input of $$('[data-scheduler-project]', form)) {
        if (input.value === '') delete projects[input.dataset.schedulerProject];
        else projects[input.dataset.schedulerProject] = Number(input.value);
      }
      if (Object.keys(projects).length) value.project_limits = projects;
      else delete value.project_limits;
      return value;
    };
    async function refresh(attempt = 0) {
      const device = state.device,
        expectedRevision = state.data.revision;
      try {
        const data = await api('/api/settings/scheduler?device=' + encodeURIComponent(device));
        if (!current() || device !== state.device || expectedRevision !== state.data.revision)
          return;
        if (data.revision !== expectedRevision && equal(data.config, state.draft)) {
          state.data = data;
          state.draft = clone(data.config);
          feedback(statusLabels[data.state] || '已保存');
        } else if (data.revision !== expectedRevision) {
          repaint(data);
          $('[data-scheduler-state]', mounted).textContent = '草稿未保存';
          feedback('其他窗口已修改设置；草稿保留，请取消改动后重新读取。');
          return;
        }
        state.data = { ...state.data, reported: data.reported, state: data.state };
        repaint(data);
        if (dirty()) $('[data-scheduler-state]', mounted).textContent = '未保存';
        if (data.state === 'pending' && attempt < 15)
          timer = setTimeout(() => refresh(attempt + 1), 2000);
      } catch (error) {
        feedback(error.message);
      }
    }
    const editDraft = () => {
      state.draft = readDraft();
      $('[data-scheduler-state]', mounted).textContent = dirty()
        ? '未保存'
        : statusLabels[state.data.state] || '待核对';
      window.CodePierSettings?.refreshDraftNote();
    };
    for (const button of $$('[data-scheduler-remove-project]', form))
      listen(button, 'click', () => {
        const input = $$('[data-scheduler-project]', form).find(
          (item) => item.dataset.schedulerProject === button.dataset.schedulerRemoveProject,
        );
        if (!input) return;
        input.value = '';
        editDraft();
        feedback('失效配额已从草稿移除；保存后生效。');
      });
    listen(form, 'input', editDraft);
    listen(form, 'change', editDraft);
    listen($('#scheduler-device', mounted), 'change', async (event) => {
      if (state.pending || (dirty() && !confirm('放弃这个节点未保存的并发设置？'))) {
        event.target.value = state.device;
        return;
      }
      state.device = event.target.value;
      state.data = null;
      state.draft = null;
      await renderPage(false);
    });
    listen($('[data-scheduler-reset]', form), 'click', async () => {
      if (state.pending) return;
      state.data = null;
      state.draft = null;
      await renderPage(false);
    });
    listen($('[data-scheduler-refresh]', form), 'click', () => {
      clearTimeout(timer);
      refresh();
    });
    listen(form, 'submit', async (event) => {
      event.preventDefault();
      if (state.pending || !form.reportValidity()) return;
      state.draft = readDraft();
      if (!dirty()) {
        feedback('没有未保存的改动');
        return;
      }
      const body = {
        device_id: state.device,
        config: clone(state.draft),
        expected_revision: state.data.revision,
      };
      state.pending = true;
      const controls = $$('input,select,button', mounted);
      controls.forEach((control) => {
        control.disabled = true;
      });
      feedback('正在保存');
      $('[data-scheduler-state]', mounted).textContent = '正在保存';
      try {
        const saved = await api('/api/settings/scheduler', {
          method: 'PUT',
          body: JSON.stringify(body),
        });
        if (!current()) return;
        state.data = saved;
        state.draft = clone(saved.config);
        repaint(saved);
        feedback(statusLabels[saved.state] || '已保存，等待核对');
        if (saved.state === 'pending') timer = setTimeout(() => refresh(), 1500);
      } catch (error) {
        if (current()) $('[data-scheduler-state]', mounted).textContent = '保存结果待核对';
        feedback(
          error.code === 'SETTINGS_CHANGED'
            ? '设置已被其他窗口修改，草稿已保留。'
            : error.message + '；请刷新状态核对结果。',
        );
      } finally {
        captured.pending = false;
        if (current())
          controls.forEach((control) => {
            control.disabled = false;
          });
      }
    });
  }
  return { html, bind, reset, detach, discard, dirty, pending: () => state.pending };
})();
