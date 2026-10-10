'use strict';
// The center is an adapter over existing APIs, never a configuration store.
window.CodePierSettings = (() => {
  const scopeLabels = {
    browser: '这个浏览器',
    user: '我的账号',
    session: '当前 CLI 会话',
    space: '当前空间',
    user_space: '我的账号 · 当前空间',
    record: '具体记录',
    project: '所选项目',
    device: '所选节点',
    device_project: '节点 · 项目',
    instance: '整个实例',
    instance_device: '实例 · 节点',
    instance_space: '实例 · 空间',
    space_project: '空间 · 项目',
    project_environment: '项目 · 环境',
  };
  const sources = {
    default: '版本默认',
    environment: '部署环境',
    database: '已有设置记录',
    agent_config: '本机 Agent 配置',
    browser_storage: '浏览器本地',
    session: '原生会话',
    running_process: '当前 Hub 进程',
    agent_runtime: '节点运行值',
    environment_agent: '部署与本机',
    database_environment: '记录与部署',
    mixed: '分别继承 / 显式设置',
    unknown: '来源待核对',
  };
  const activation = {
    immediate: '立即生效',
    new_consent: '下一次授权预选',
    session_receipt: '以会话回执为准',
    new_call: '下一次调用',
    validated: '节点验证后生效',
    operation_receipt: '以操作回执为准',
    deployment: '更新部署后核对',
    reconnect: '本机热载并重连',
    new_session: '新会话 / 实例生效',
    restart: '需要重启并核对',
    approved_plan: '单独批准后生效',
    new_connection: '重新核对连接授权',
    provider_policy: '按提供者来源生效',
  };

  const views = [
    ['', '开始'],
    ['devices', '设备'],
    ['projects', '项目'],
    ['connect', '接入'],
    ['files', '文件'],
    ['tasks', '任务'],
    ['usage', '用量'],
    ['advanced', '高级'],
  ];
  const viewItems = {
    devices: ['device', 'roots'],
    projects: ['project_mapping', 'vps', 'sharing', 'artifacts'],
    connect: ['grants', 'access_defaults', 'profiles', 'gateway', 'oauth_policy', 'public_url'],
    files: [
      'hub_ingress',
      'native_relay',
      'hub_file_sources',
      'node_ingress',
      'node_file_sources',
      'artifacts',
    ],
    tasks: [
      'native_session',
      'delegation',
      'shell',
      'skills',
      'language_servers',
      'worktrees',
      'browser',
      'computer',
      'local_control',
      'execution_policy',
      'collaboration_features',
      'collaboration_access',
      'monitor',
      'reliability',
    ],
    usage: ['usage_privacy', 'appearance'],
    advanced: [
      'account_security',
      'spaces',
      'roles',
      'oidc',
      'users',
      'hub_process',
      'maintenance',
    ],
  };
  const mappedItems = new Set(Object.values(viewItems).flat());
  const commonItems = new Set([
    'device',
    'project_mapping',
    'grants',
    'access_defaults',
    'native_session',
    'delegation',
    'usage_privacy',
    'appearance',
    'account_security',
    'spaces',
  ]);
  function startHTML() {
    const steps = [
      ['01', '连接节点', '让电脑与面板连通', 'devices', 'devices'],
      ['02', '添加项目', '选择允许工作的目录', 'projects', 'projects'],
      ['03', '接入工具', '连接客户端并确认范围', 'connect', 'connect'],
      ['04', '派发任务', '开始工作并查看进度', 'collaboration', 'tasks'],
      ['05', '查看用量', '查看调用、Token 与费用', 'overview', 'usage'],
    ];
    return (
      '<section id="settings-start" aria-label="设置流程"><div class="settings-flow-grid">' +
      steps
        .map(
          ([number, title, description, entry, view]) =>
            '<article class="settings-flow-card"><span class="settings-step">' +
            number +
            '</span><h2>' +
            title +
            '</h2><p>' +
            description +
            '</p><div class="actions"><button type="button" class="btn primary" data-settings-entry="' +
            entry +
            '">打开</button><button type="button" class="btn ghost" data-settings-group="' +
            view +
            '">相关设置</button></div></article>',
        )
        .join('') +
      '</div><div class="settings-shortcuts"><button type="button" class="btn" data-settings-group="files">文件传输</button>' +
      '<button type="button" class="btn" data-settings-group="tasks">并发与排队</button>' +
      '<button type="button" class="btn ghost" data-settings-group="advanced">账号与维护</button></div></section>'
    );
  }

  let state = {
      owner: '',
      project: '',
      group: '',
      query: '',
      data: null,
      draft: {},
      base: {},
      node: null,
      pending: false,
      batchApply: false,
    },
    generation = 0,
    controller = null,
    root = null;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const owner = () => (S.session?.user_id || S.session?.username || '') + ':' + S.space_id;
  function dirty() {
    return (
      state.batchApply ||
      !!window.CodePierFileImportSettings?.dirty() ||
      !!window.CodePierSchedulerSettings?.dirty() ||
      Object.keys(state.draft).some((key) => !equal(state.draft[key], state.base[key]))
    );
  }
  function refreshDraftNote() {
    const note = root?.querySelector('#settings-draft-note');
    if (note) note.hidden = !dirty();
  }
  function reset() {
    window.CodePierFileImportSettings?.reset();
    window.CodePierSchedulerSettings?.reset();
    detach();
    state = {
      owner: '',
      project: '',
      group: '',
      query: '',
      data: null,
      draft: {},
      base: {},
      node: null,
      pending: false,
      batchApply: false,
    };
  }
  function detach() {
    window.CodePierFileImportSettings?.detach();
    window.CodePierSchedulerSettings?.detach();
    generation++;
    controller?.abort();
    controller = null;
    root = null;
  }
  function requestLeave() {
    if (
      state.pending ||
      window.CodePierFileImportSettings?.pending() ||
      window.CodePierSchedulerSettings?.pending()
    ) {
      toast('设置保存仍在核对，请等待回执或重新读取后再离开。', true);
      return false;
    }
    if (!dirty()) return true;
    if (!confirm('设置有未保存的改动。放弃这些草稿并离开？')) return false;
    state.draft = clone(state.base);
    state.batchApply = false;
    window.CodePierFileImportSettings?.discard();
    window.CodePierSchedulerSettings?.discard();
    return true;
  }
  function hydrate(data, replace = false) {
    state.data = data;
    const values = {
      appearance: window.CodePierAppearance?.getPreference() || 'auto',
      access_defaults: data.items.find((item) => item.id === 'access_defaults').effective_value,
      public_url: data.items.find((item) => item.id === 'public_url').effective_value,
    };
    for (const [key, value] of Object.entries(values)) {
      if (replace || !(key in state.base) || equal(state.draft[key], state.base[key])) {
        state.base[key] = clone(value);
        state.draft[key] = clone(value);
      }
    }
  }
  const buttons = (key) =>
    '<div class="actions"><button class="btn primary" type="submit">保存并核对</button><button class="btn ghost" type="button" data-settings-cancel>取消改动</button></div><p class="form-note settings-save-status" ' +
    (key === 'access_defaults' ? 'id="access-settings-status" ' : '') +
    'role="status"></p>';
  function editor(item) {
    if (!item.editable_here) return '';
    if (item.editor === 'file_import')
      return '<button type="button" class="btn primary" data-file-import-open>编辑文件入站设置</button>';
    const value = state.draft[item.id];
    if (item.id === 'appearance')
      return (
        '<form data-settings-form="appearance"><label class="field">外观<select name="appearance">' +
        [
          ['auto', '跟随系统'],
          ['light', '浅色'],
          ['dark', '深色'],
        ]
          .map(
            ([id, label]) =>
              '<option value="' +
              id +
              '" ' +
              (id === value ? 'selected' : '') +
              '>' +
              label +
              '</option>',
          )
          .join('') +
        '</select></label>' +
        buttons(item.id) +
        '</form>'
      );
    if (item.id === 'access_defaults')
      return (
        '<form id="access-settings-form" data-settings-form="access_defaults"><div class="check-list"><label class="check"><input name="all_projects" type="checkbox" ' +
        (value.all_projects ? 'checked' : '') +
        '>默认选择全部现有及未来项目</label><label class="check"><input name="developer_scopes" type="checkbox" ' +
        (value.developer_scopes ? 'checked' : '') +
        '>默认勾选读取、写入和执行权限</label></div>' +
        '<p class="form-note">这里只保存你的预选，不批量更改已有连接。已有授权请到 MCP 接入逐项核对。</p>' +
        buttons(item.id) +
        '</form>'
      );
    if (item.id === 'public_url')
      return (
        '<form id="settings-form" data-settings-form="public_url"><label class="field">基础地址<input name="public_url" type="url" maxlength="500" required value="' +
        esc(value) +
        '"></label><p class="form-note">HTTP / HTTPS 根地址，不带 /mcp。</p>' +
        buttons(item.id) +
        '</form>'
      );
    return '';
  }
  function advancedHTML() {
    const enabled = state.base.access_defaults.all_projects;
    return (
      '<details class="settings-details" id="access-settings-advanced"><summary>高级：已有 OAuth 连接的项目范围</summary><form id="access-batch-form"><p>先保存“默认选择全部项目”，再单独确认是否扩大当前账号已有有效传统 OAuth 的项目范围。不影响 PAT、Profile、工具权限或凭据有效期。</p>' +
      '<label class="check"><input type="checkbox" name="apply_to_existing" ' +
      (state.batchApply ? 'checked' : '') +
      (enabled ? '' : ' disabled') +
      '>我希望将全部现有及未来项目应用到已有传统 OAuth 连接</label><button type="submit" class="btn">单独确认并批量应用</button><p id="access-batch-status" class="form-note" role="status"></p></form></details>'
    );
  }
  function bindAdvanced(mounted, current, listen) {
    const form = $('#access-batch-form', mounted);
    if (!form) return;
    const checkbox = form.elements.apply_to_existing;
    listen(checkbox, 'change', () => {
      state.batchApply = checkbox.checked;
    });
    listen(form, 'submit', async (event) => {
      event.preventDefault();
      if (state.pending || !checkbox.checked) return;
      if (!equal(state.draft.access_defaults, state.base.access_defaults)) {
        $('#access-batch-status', form).textContent =
          '请先保存或取消上面的个人预选草稿，再进行批量操作。';
        return;
      }
      if (
        !confirm(
          '将当前账号所有有效传统 OAuth 连接（不含 Profile）改为可访问全部现有及未来新增项目？\n只扩展项目范围，现有工具权限、PAT、凭据和有效期保持不变。',
        )
      )
        return;
      const captured = state;
      captured.pending = true;
      $('button', form).disabled = true;
      checkbox.disabled = true;
      try {
        const result = await api('/api/settings/access', {
          method: 'PUT',
          body: JSON.stringify({
            ...captured.base.access_defaults,
            all_projects: true,
            apply_to_existing: true,
            expected_defaults: captured.base.access_defaults,
          }),
        });
        if (!current()) return;
        const fresh = await api(
          '/api/settings/catalog' +
            (captured.project ? '?project=' + encodeURIComponent(captured.project) : ''),
        );
        if (!current()) return;
        if (!fresh.items.find((item) => item.id === 'access_defaults').effective_value.all_projects)
          throw new Error('重新读取的默认值已变化，请核对原连接范围。');
        hydrate(fresh);
        captured.batchApply = false;
        checkbox.checked = false;
        checkbox.defaultChecked = false;
        $('#access-batch-status', form).textContent =
          result.note + ' 已更新 ' + result.updated_grants + ' 个连接，无需重新连接。';
      } catch (error) {
        if (current())
          $('#access-batch-status', form).textContent =
            error.message + '；请核对原连接，不自动重试。';
      } finally {
        captured.pending = false;
        if (current()) {
          $('button', form).disabled = false;
          checkbox.disabled = !state.base.access_defaults.all_projects;
          refreshDraftNote();
        }
      }
    });
  }
  function valueText(item) {
    if (item.state === 'restricted') return '仅管理员可查看部署细节';
    if (item.state === 'select_project') return '选择项目后检查节点';
    if (item.state === 'not_checked') return '本机配置尚未检查';
    if (item.state === 'browser') return '保存在当前浏览器';
    if (item.state === 'invalid') return '配置无效，功能保持受限';
    if (item.effective_value === null) return item.entry ? '在原管理入口核对' : '尚未核对';
    if (typeof item.effective_value === 'boolean')
      return item.effective_value ? '已开启' : '已关闭';
    return typeof item.effective_value === 'string'
      ? item.effective_value
      : JSON.stringify(item.effective_value, null, 2);
  }

  function card(item) {
    const content = item.editor === 'file_import' ? '' : editor(item);
    const mode = item.editable_here
      ? '可编辑'
      : item.edit_mode === 'local'
        ? '本机设置'
        : item.edit_mode === 'deployment'
          ? '部署设置'
          : item.edit_mode === 'restricted'
            ? '管理员'
            : '';
    const entry =
      item.id === 'usage_privacy'
        ? '<button type="button" class="btn" data-devtools="status" data-project="' +
          esc(state.project) +
          '">查看项目用量</button>'
        : item.entry
          ? '<button type="button" class="btn" data-settings-entry="' +
            esc(item.entry) +
            '">打开管理</button>'
          : '';
    return (
      '<article class="panel settings-card" id="setting-' +
      esc(item.id) +
      '" data-setting-id="' +
      esc(item.id) +
      '" data-setting-group="' +
      esc(item.group) +
      '">' +
      '<div class="panel-head"><h2>' +
      esc(item.title) +
      '</h2><span class="settings-mode">' +
      esc(mode) +
      '</span></div><div class="panel-body"><div class="settings-tags"><span>' +
      esc(scopeLabels[item.scope] || item.scope) +
      '</span></div>' +
      content +
      entry +
      (item.id === 'access_defaults' && state.data.space_admin ? advancedHTML() : '') +
      '<details class="settings-details"><summary>详情</summary><p>' +
      esc(item.description) +
      '</p><pre class="settings-value" data-setting-value>' +
      esc(valueText(item)) +
      '</pre><div class="settings-tags"><span data-setting-source>' +
      esc(sources[item.source] || item.source) +
      '</span><span>' +
      esc(activation[item.activation] || item.activation) +
      '</span></div><p>管理者：' +
      esc(item.owner) +
      '</p><p>' +
      esc(item.fields.join(' · ')) +
      '</p>' +
      (item.configured_value !== null
        ? '<p>已配置：' + esc(JSON.stringify(item.configured_value)) + '</p>'
        : '') +
      '</details></div></article>'
    );
  }
  function chainHTML() {
    const data = state.data,
      rows = Object.fromEntries(data.items.map((item) => [item.id, item]));
    const node = state.node || data.node;
    const bool = (v) => (v === true ? '已开启' : v === false ? '已关闭' : '未核对');
    const status =
      {
        select_project: '先选择项目',
        not_checked: '尚未检查',
        offline: '节点离线，值未知',
        pending: '检查进行中，保留原回执',
        failed: '检查失败，值未知',
        unknown: '节点未返回此能力',
        checked: '已读取节点运行值',
      }[node.state] || '未核对';
    return (
      '<section class="panel settings-file-chain" aria-label="文件入站分层状态"><div class="panel-head"><h2>文件能否送到项目？</h2></div><div class="panel-body"><ol class="settings-chain">' +
      '<li><strong>1. Hub 接收</strong><span>' +
      bool(rows.hub_ingress.effective_value) +
      '</span></li>' +
      '<li><strong>2. 原生附件转发</strong><span>' +
      bool(rows.native_relay.effective_value) +
      '</span></li>' +
      '<li><strong>3. 目标节点接收</strong><span>' +
      bool(node.values?.node_ingress) +
      '</span></li>' +
      '<li><strong>4. 项目写入</strong><span>' +
      esc(
        { ready: '已获准', denied: '受限', paused: '已暂停' }[node.values?.project_write] ||
          '未核对',
      ) +
      '</span></li></ol>' +
      '<details class="settings-details"><summary>这些状态代表什么？</summary><p>接收与写入许可分别检查，附件来源也必须获准。就绪状态不能代替真实文件上传回执。</p></details>' +
      '<div class="actions"><button type="button" class="btn" id="settings-check-node" ' +
      (!state.project ? 'disabled' : '') +
      '>' +
      (node.state === 'pending' ? '查询原检查' : '检查所选节点') +
      '</button><span id="settings-node-state" role="status">' +
      esc(status) +
      '</span></div>' +
      (node.operation_id
        ? '<p class="form-note">检查回执：<code>' + esc(node.operation_id) + '</code></p>'
        : '') +
      '</div></section>'
    );
  }
  async function html() {
    const session = S.session,
      space = S.space_id;
    if (state.owner !== owner()) {
      reset();
      state.owner = owner();
    }
    const expectedOwner = state.owner,
      ticket = ++generation;
    const data = await api(
      '/api/settings/catalog' +
        (state.project ? '?project=' + encodeURIComponent(state.project) : ''),
    );
    if (
      ticket !== generation ||
      S.session !== session ||
      S.space_id !== space ||
      expectedOwner !== owner()
    )
      throw sessionChanged();
    hydrate(data);
    const fileEditor = data.instance_admin ? await CodePierFileImportSettings.html() : '';
    let schedulerEditor;
    try {
      schedulerEditor = await CodePierSchedulerSettings.html(state.project);
    } catch (error) {
      schedulerEditor =
        '<section id="settings-scheduler" class="panel" role="status"><div class="panel-body">并行设置暂不可用：' +
        esc(error.message) +
        '</div></section>';
    }
    if (ticket !== generation || S.session !== session || S.space_id !== space)
      throw sessionChanged();

    return (
      heading('设置中心', 'SETTINGS', '按你要做的事开始。') +
      '<section id="settings-center" class="settings-center"><div class="settings-toolbar">' +
      '<label class="field">搜索设置<input id="settings-search" type="search" placeholder="搜索功能或设置…" value="' +
      esc(state.query) +
      '"></label><label class="field" id="settings-project-field">项目<select id="settings-project">' +
      '<option value="">选择项目</option>' +
      data.projects
        .map(
          (p) =>
            '<option value="' +
            esc(p.id) +
            '" ' +
            (p.id === state.project ? 'selected' : '') +
            '>' +
            esc(p.alias) +
            (p.online ? '' : ' · 离线') +
            '</option>',
        )
        .join('') +
      '</select></label></div>' +
      '<nav class="settings-groups" aria-label="设置场景">' +
      views
        .map(
          ([id, title]) =>
            '<button type="button" data-settings-group="' + id + '">' + title + '</button>',
        )
        .join('') +
      '</nav><p id="settings-draft-note" class="settings-draft-note" hidden role="status">有未保存的改动</p>' +
      '<p id="settings-filter-status" class="form-note" role="status"></p>' +
      startHTML() +
      '<div id="settings-chain">' +
      chainHTML() +
      '</div>' +
      (fileEditor
        ? '<details id="settings-file-editor-wrap" class="settings-section"><summary>接收与来源设置</summary>' +
          fileEditor +
          '</details>'
        : '') +
      schedulerEditor +
      '<div class="settings-grid settings-catalog settings-common">' +
      data.items
        .filter((item) => commonItems.has(item.id))
        .map(card)
        .join('') +
      '</div>' +
      '<details id="settings-more" class="settings-section"><summary>更多设置 <span id="settings-more-count"></span></summary>' +
      '<div class="settings-grid settings-catalog">' +
      data.items
        .filter((item) => !commonItems.has(item.id))
        .map(card)
        .join('') +
      '</div></details><p id="settings-no-results" hidden>没有匹配的设置</p>' +
      '<div id="settings-all-entry" hidden><button type="button" class="btn ghost" data-settings-group="all">查看全部设置</button></div>' +
      (data.instance_admin
        ? '<details class="settings-maintenance settings-section" aria-label="面板维护"><summary>面板更新与诊断</summary>' +
          CodePierPanelUpdate.html() +
          '</details>'
        : '') +
      '</section>'
    );
  }
  function bind() {
    detach();
    root = $('#settings-center');
    if (!root) return;
    controller = new AbortController();
    const mounted = root,
      mountedState = state,
      session = S.session,
      space = S.space_id,
      ticket = generation,
      signal = controller.signal;
    const current = () =>
      !signal.aborted &&
      mounted.isConnected &&
      root === mounted &&
      ticket === generation &&
      S.session === session &&
      S.space_id === space &&
      S.page === 'settings';
    const listen = (node, event, fn) => node?.addEventListener(event, fn, { signal });
    const feedback = (form, text) => {
      if (current()) $('.settings-save-status', form).textContent = text;
    };
    CodePierFileImportSettings.bind();
    window.CodePierSchedulerSettings?.bind();

    function filter() {
      const q = state.query.trim().toLocaleLowerCase();
      const selected = new Set(viewItems[state.group] || []);
      let count = 0,
        advancedCount = 0;
      for (const item of state.data.items) {
        const text = [
          item.title,
          item.description,
          item.owner,
          scopeLabels[item.scope],
          sources[item.source],
          ...item.fields,
        ]
          .join(' ')
          .toLocaleLowerCase();
        const shown = q
          ? text.includes(q)
          : state.group === 'all' ||
            selected.has(item.id) ||
            (state.group === 'advanced' && !mappedItems.has(item.id));
        const node = $('[data-setting-id="' + item.id + '"]', mounted);
        if (node) node.hidden = !shown;
        if (shown) {
          count++;
          if (!commonItems.has(item.id)) advancedCount++;
        }
      }
      $$('button[data-settings-group]', mounted).forEach((button) =>
        button.setAttribute('aria-pressed', String(button.dataset.settingsGroup === state.group)),
      );
      $('#settings-start', mounted).hidden = !!state.group || !!q;
      $('#settings-project-field', mounted).hidden =
        !q && !['devices', 'projects', 'files', 'tasks', 'usage', 'all'].includes(state.group);
      $('#settings-draft-note', mounted).hidden = !dirty();
      const more = $('#settings-more', mounted);
      more.hidden = !advancedCount;
      more.open = !!q || state.group === 'all';
      $('#settings-more-count', mounted).textContent = advancedCount
        ? '(' + advancedCount + ')'
        : '';
      const schedulerEditor = $('#settings-scheduler', mounted);
      const schedulerVisible =
        !!schedulerEditor &&
        (q
          ? /并行|并发|排队|调度|scheduler|concurrency/.test(q)
          : state.group === 'tasks' || state.group === 'all');
      if (schedulerEditor) schedulerEditor.hidden = !schedulerVisible;
      count += schedulerVisible ? 1 : 0;
      const filesVisible = q
        ? /文件|入站|file|import|stream|relay|provider|hosts/.test(q)
        : state.group === 'files' || state.group === 'all';
      $('#settings-chain', mounted).hidden = !filesVisible;
      const fileEditor = $('#settings-file-import-editor', mounted);
      if (fileEditor) fileEditor.hidden = !filesVisible;
      const fileWrap = $('#settings-file-editor-wrap', mounted);
      if (fileWrap) {
        fileWrap.hidden = !filesVisible;
        if (q && filesVisible) fileWrap.open = true;
      }
      $('#settings-filter-status', mounted).textContent = q ? '找到 ' + count + ' 项设置' : '';
      $('#settings-no-results', mounted).hidden = !q || count !== 0;
      $('#settings-all-entry', mounted).hidden = !!q || state.group !== 'advanced';
      const maintenance = $('.settings-maintenance', mounted);
      if (maintenance)
        maintenance.hidden = q
          ? !/更新|版本|维护|update/.test(q)
          : !['advanced', 'all'].includes(state.group);
    }
    listen($('#settings-search', mounted), 'input', (event) => {
      state.query = event.target.value;
      filter();
    });
    for (const button of $$('button[data-settings-group]', mounted))
      listen(button, 'click', () => {
        if (
          state.pending ||
          CodePierFileImportSettings.pending() ||
          window.CodePierSchedulerSettings?.pending()
        ) {
          toast('正在保存，请稍候。', true);
          return;
        }
        state.group = button.dataset.settingsGroup;
        state.query = '';
        $('#settings-search', mounted).value = '';
        filter();
      });
    listen($('#settings-project', mounted), 'change', async (event) => {
      const next = event.target.value;
      if (!requestLeave()) {
        event.target.value = state.project;
        return;
      }
      state.project = next;
      state.node = null;
      await renderPage(false);
      $('#settings-project')?.focus();
    });
    for (const button of $$('[data-settings-entry]', mounted))
      listen(button, 'click', async () => {
        const destination = button.dataset.settingsEntry;
        if (!requestLeave()) return;
        await navigate(destination);
      });
    for (const button of $$('[data-file-import-open]', mounted))
      listen(button, 'click', () => {
        state.group = 'files';
        state.query = '';
        $('#settings-search', mounted).value = '';
        filter();
        const wrap = $('#settings-file-editor-wrap', mounted);
        if (wrap) wrap.open = true;
        $('#settings-file-import-editor', mounted)?.scrollIntoView({
          behavior: 'smooth',
          block: 'start',
        });
        $('#file-import-settings-form select', mounted)?.focus({ preventScroll: true });
      });
    for (const button of $$('[data-devtools]', mounted))
      listen(button, 'click', (event) => {
        if (!requestLeave()) {
          event.preventDefault();
          event.stopPropagation();
        }
      });
    listen(mounted, 'input', refreshDraftNote);
    listen(mounted, 'change', refreshDraftNote);
    bindAdvanced(mounted, current, listen);
    for (const form of $$('[data-settings-form]', mounted)) {
      const key = form.dataset.settingsForm;
      const read = () =>
        key === 'appearance'
          ? form.elements.appearance.value
          : key === 'public_url'
            ? form.elements.public_url.value
            : {
                all_projects: form.elements.all_projects.checked,
                developer_scopes: form.elements.developer_scopes.checked,
              };
      listen(form, 'input', () => {
        state.draft[key] = read();
      });
      listen(form, 'change', () => {
        state.draft[key] = read();
      });
      listen($('[data-settings-cancel]', form), 'click', async () => {
        state.draft[key] = clone(state.base[key]);
        await renderPage(false);
      });
      listen(form, 'submit', async (event) => {
        event.preventDefault();
        if (!form.reportValidity() || state.pending) return;
        state.draft[key] = read();
        const submitted = clone(state.draft[key]),
          baseline = clone(state.base[key]);
        if (equal(submitted, baseline)) {
          feedback(form, '没有需要保存的改动。');
          return;
        }
        if (
          key === 'public_url' &&
          !confirm(
            '将 MCP / OAuth 对外标识改为 ' +
              submitted +
              '？现有连接可能需要重新授权；不会修改监听或 Agent 地址。',
          )
        )
          return;
        state.pending = true;
        const controls = $$('input, select, button', form);
        controls.forEach((control) => {
          control.disabled = true;
        });
        feedback(form, '正在保存并重新读取有效值…');
        try {
          if (key === 'appearance') {
            window.CodePierAppearance.setPreference(submitted);
            if (window.CodePierAppearance.getPreference() !== submitted)
              throw new Error('外观未生效，请重新核对。');
            if (window.localStorage.getItem('codepier-appearance') !== submitted)
              throw new Error('外观已应用，但浏览器未保存偏好；请检查本地存储限制。');
            state.base[key] = submitted;
          } else {
            const saved = await api(
              key === 'public_url' ? '/api/settings' : '/api/settings/access',
              {
                method: 'PUT',
                body: JSON.stringify(
                  key === 'public_url'
                    ? { public_url: submitted, expected_public_url: baseline }
                    : { ...submitted, apply_to_existing: false, expected_defaults: baseline },
                ),
              },
            );
            if (!current()) return;
            const fresh = await api(
              '/api/settings/catalog' +
                (state.project ? '?project=' + encodeURIComponent(state.project) : ''),
            );
            if (!current()) return;
            const actual = fresh.items.find((item) => item.id === key).effective_value;
            const wanted = key === 'public_url' ? saved.public_url : submitted;
            if (!equal(actual, wanted))
              throw new Error('保存后有效值与草稿不同；请核对其他窗口的更改，草稿已保留。');
            state.data = fresh;
            state.base[key] = clone(actual);
            state.draft[key] = clone(actual);
            S.settings = await api('/api/settings');
            if (!current()) return;
          }
          for (const field of $$('input', form)) {
            if (field.type === 'checkbox') field.defaultChecked = field.checked;
            else field.defaultValue = field.value;
          }
          const batch = $('#access-batch-form', mounted);
          if (batch)
            $('[name="apply_to_existing"]', batch).disabled =
              !state.base.access_defaults.all_projects;
          feedback(
            form,
            (key === 'access_defaults' ? '默认选项已保存，' : '已保存，') + '并重新核对生效值。',
          );
          toast('设置已保存并核对');
        } catch (error) {
          feedback(
            form,
            error.code === 'SETTINGS_CHANGED'
              ? '设置已在其他窗口修改。你的草稿仍保留，请取消改动后重新读取，或核对当前有效值。'
              : error.code === 'NETWORK_UNCERTAIN'
                ? '回执暂未确认，草稿已保留。请重新读取核对，不要假定保存失败。'
                : error.message,
          );
        } finally {
          mountedState.pending = false;
          if (current()) {
            controls.forEach((control) => {
              control.disabled = false;
            });
            refreshDraftNote();
          }
        }
      });
    }
    function updateNode() {
      $('#settings-chain', mounted).innerHTML = chainHTML();
      const button = $('#settings-check-node', mounted);
      listen(button, 'click', async () => {
        button.disabled = true;
        $('#settings-node-state', mounted).textContent = '正在查询当前项目的节点状态…';
        try {
          const result = await api(
            '/api/settings/node?project=' + encodeURIComponent(state.project),
            { signal, requestTimeout: 15000 },
          );
          if (!current()) return;
          state.node = result;
          for (const [key, value] of Object.entries(result.values || {})) {
            const item = state.data.items.find((entry) => entry.id === key);
            if (!item) continue;
            item.effective_value = value;
            item.state = value === null ? 'unknown' : 'known';
            item.source = 'agent_runtime';
            const card = $('[data-setting-id="' + key + '"]', mounted);
            if (card) {
              $('[data-setting-value]', card).textContent = valueText(item);
              $('[data-setting-source]', card).textContent = sources.agent_runtime;
            }
          }
          updateNode();
        } catch (error) {
          if (current()) {
            $('#settings-node-state', mounted).textContent = error.message;
            button.disabled = false;
          }
        }
      });
    }
    updateNode();
    filter();
    if (state.data.instance_admin) CodePierPanelUpdate.bind();
  }
  window.addEventListener('beforeunload', (event) => {
    if (dirty() || state.pending || window.CodePierSchedulerSettings?.pending()) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  return { html, bind, detach, reset, dirty, requestLeave, refreshDraftNote };
})();
